# Kodak Charmera (GPCV2247F) stock firmware: boot path, power management, clocks, battery

Static analysis only. Dump: `fw/keychain_stock.bin` (2 MiB, sha256 cc3b4b2f...ea42f), not modified.
Scripts: `work/bootpower/fwlib.py` (capstone helpers, call graph), `d.py <file-off-hex> <n>` (annotated disassembly),
`strs.py`, dumps `full_arm.txt` (whole code region, ARM mode), `f_*.txt` (per-function dumps).
All addresses below are **file offsets** unless marked VA. Labels: VERIFIED (seen in the binary), INFERRED, UNKNOWN.

## 0. Address mapping (VERIFIED)

* Vector table at 0x200: `ldr pc,[pc,#0x18]` reads 0x220 = 0x0000003C; file 0x23C holds `msr cpsr_fsxc,#0xd3` (classic reset entry). So runtime VA = file - 0x200.
* Cross-check 1: literal pools in the high region hold VAs of strings, e.g. 0x4662c holds 0xF45E4 = file 0xF47E4 "Assert!! file: %s, function: %s, line: %d".
* Cross-check 2: low-region code uses `adr` (`add rX, pc, #imm`): 0x63D4 `add r0,pc,#0x168` -> 0x6544 "POWER_ON_LOGO.JPG"; 0x6404 -> 0x6558 "state_startup_init"; 0x230F0 -> 0x23318 "ap_state_handling_power_off"; 0x1E448 -> 0x1E4A0 "POWERON_AUDIO.WAV".
* Whole image is ARM mode (no Thumb found). Compiler: RVCT/ADS style (adr for local strings, literal pools).
* Reset handler (0x23C..0x364): zero 0xFF000000, 0xD0100038=1, 0xD0300000/40/80/C0=0x80000000, 0xD05001FC=0, 0xD07023C0=0; .data copy 0xFF870->0xFF870 (0xB6C0 bytes, same address so no copy), .bss zero 0x10AF30..0x1AE750; per-mode stacks; `b 0x2DD48` (main) with r0 = stack top.

SPII header words (file 0x00..0x44): `SPII`, 0x30000200, 0x20000000, 0, 0x001B0000, `SRIy`, 0x01000510, 0x00010001, 0xD0667802, 5,
then register pairs (0xD0000088,0xA12B) (0xD000008C,0x2495) (0xD0000028,0x8005) (0xD00000AC,0x104).
VERIFIED that 0xA12B/0x2495 to 0xD0000088/8C are *unlock keys*: the firmware's own helper 0xDF2F4 writes exactly this pair immediately before every write to 0xD0000028 (callers 0x30D54, 0x30D74):

```
0df2f4  ldr r1,=0xa12b ; mov r0,#0xd0000000 ; str r1,[r0,#0x88]
0df300  ldr r1,=0x2495 ; str r1,[r0,#0x8c] ; mov pc,lr
030d54  (set bit15 of 0xD0000028): ldr r1,[r5,#0x28]; bic r1,#0x8000; orr r4,r1,r0,lsl#15; bl 0xdf2f4; str r4,[r5,#0x28]
030d74  (set bits2:0 of 0xD0000028): ldr r1,[r5,#0x28]; bic r1,#7; orr r4,r1,r0; bl 0xdf2f4; str r4,[r5,#0x28]
```

## 1. OS, tasks, timers

### 1.1 RTOS = uC/OS-II (VERIFIED)
* 0x9012C OSTaskCreate (prio > 0x3F -> error 0x2A = OS_PRIO_INVALID), 0x906E4 OSTimeDly(ticks) (stores ticks to TCB+0x2A, calls OS_Sched 0x8D160), 0x90734 OSTimeDlyHMSM (error codes 0x51..0x54; ticks = (100*ms+500)/1000 -> **OS tick = 100 Hz VERIFIED**), 0x8D848 OSStart, 0x8D880 OSTimeTick, 0x908B8 OSTimeGet, 0x8F87C OSQPost, 0x8F760 OSQPend, 0xEFF84/0xEFFA4 enter/exit critical. String "uC/OS-II Idle" at 0x8D708.
* main (0x2DD48): 0x31264; 0x36254(0); 0x8CF28 (OSInit, INFERRED); 0x2CE24; 0x36274 (clock/system init, see §4); reads GPIO 8/9 via 0x2E96C, if both 0 sets 0x2EBA4(8,0x2A), (9,6), 0x325F0(0x28) (busy-wait); 0x2DCA8(0); 0x668D8(5,6,4); heap descriptor at VA 0x11A2D4 = {stack_top rounded, 0x800000} -> 8 MiB heap (VERIFIED), 0x7D8 heap init; **0x31B4C(timer0, 100, 0, isr=VA 0x8D680)** OS tick; OSTaskCreate(root VA 0x2DAAC, stack VA 0x11AAD8, prio 0x35); 0x31264 again; OSStart.
* Root task 0x2DCAC creates 7 tasks then OSTaskDel(self):

| prio | entry (file) | role (how known) | queue |
|---|---|---|---|
| 1 | 0x12478 | file/storage service (msgs 0x31xxxxxx) INFERRED | 0x10F1FC |
| 7 | 0x16DD4 | display/LCD service: init 0x16CE8 -> 0x165AC allocates frames and sets display-ready byte 0x10FBC8=1 at 0x1669C (VERIFIED) | 0x10FD58+4 / 0x10FD5C |
| 0x11 | 0x13310 | audio playback (WAV) service, msgs 0x100001xx (INFERRED from 0x1EA44 path) | 0x10F25C |
| 0x2D | 0x25868 | UI state task: dispatches state msgs 0x100..0x10D (VERIFIED) | 0x110D90 (states read 0x110D88) |
| 0x30 | 0x8758 | peripheral task: keys, ADC/battery, countdowns, auto-off (VERIFIED) | 0x10CDA0 |
| 0x32 | 0x36B70 | USB (PC_CAM/U_DISK) detect task (INFERRED) | 0x11FA68+4 |
| 0x34 | 0x5EF0 | "AP" task: storage/SD detection, DCIM, 1-s housekeeping (0x303C) (VERIFIED content) | 0x10C3E4 |

Message encoding: msgQSend(q, 0xNNMM) packs as (0xNN<<24)|0xMM; states compare against 0x7000xxxx (UI events), 0x5000xxxx (AP), 0x6000xxxx (peripheral).

### 1.2 Software timer service (0x27CAC) and its tick unit
`0x27CAC(fn, queue, msg, slot<=6, period)` stores a 5-word record at VA 0x110F38+slot*0x14 and starts hardware timer 2 once (0x27D80 -> 0x32504(2,0,0,cb=VA 0x27920)). The timer ISR (0xDFE54) calls the callback 0x27B20 which decrements each slot counter and, at 0, posts 0x10000<<slot to the service queue 0x110F30 whose consumer (0x27954) calls fn(queue,msg).

Tick unit = 1/128 s (**INFERRED, strong**). Evidence:
* loop-record timer (0x1C440): `movne r1,#120,#26` = 7680 ticks x minutes (table 0xF3B58 = {0,1,2,3,5,10} min), else 0x38380 = 230272 ticks = exactly 1799 s (29:59) at 128 Hz.
* AP housekeeping tick 0x5001 period 0x80 (0x1F94) = 1.000 s; 0x5002 period 0x500 = 10 s.
* peripheral periodic message 0x6000 period 4 (0x6D84) = 31.25 ms; the idle-time code does `idle += 4` per run and `idle >> 7` for seconds (= 32 runs x 31.25 ms = 1 s exactly, 0x7768/0x7770).
* power-key long press = 48 runs (0x74E8 `cmp r3,#0x30`) = 1.5 s; settings-flush check every 160 runs (0x7FFC `cmp r0,#0xa0`) = 5 s; counter 0x60 (0x8168) = 3 s.
Hardware timer 2 control = 0xC0030000+8 (ctrl bits 13/14/15 set, low field = r1 = 0); the fixed rate select is not in the firmware (0x325E0 writes 0xC0030020=0x5555 but has no callers), so the absolute rate rests on the inference above. If the real rate differed, every "seconds" value in §3 scales accordingly (the owner's auto-off stopwatch test, §6, resolves it).

## 2. Boot path (question 1)

Order of events, with blocking points.

1. Reset -> main -> OSInit -> OSStart; nothing sleeps here (VERIFIED). Clock setup in main is register writes only (§4). SDRAM: the 8 MiB heap starts right after the stacks; no SDRAM controller init was identified in main (the boot ROM/SPII header must have done it; UNKNOWN).
2. Root task creates the 7 tasks; the highest-priority task runs first. The state task (prio 0x2D) init 0x24F7C: creates queues, 0x1B8C (sets SPI flash clock divider for 36 MHz: 0x3017C(0x2255100)), 0x1FD24 **settings load** (0x1CE8(0,buf,1) -> 0x1BD0 flash read of a 0xFC-byte record + CRC32 compare 0x1FB50/0x1FB0C), defaults 0x1F89C on CRC failure, 0x23ED0 resource init (ap_state_resource_init), 0x8D1C, 0x238A4, then state 0x222CC(0xFF). The state task then waits for a state message; who posts the first 0x100 was not traced (INFERRED: 0x24F7C/0x222CC tail), state_startup runs first (handler 0x2591C).
3. **state_startup** 0x65B0 (msg 0x100), VERIFIED, calls in order:
   * 0x6104 boot battery check: direct ADC read at 0xC00D0000 (trigger 0x70, wait bit31, read +8 three times, value>>4); if 0x47F <= adc < 0x5AA it writes "register 0x83" = 0xA5 via 0x2EBA4 (low-battery marker read back by 0x62A8). Values outside that window do nothing.
   * 0x1E434 resolves CLICK/POWERON/POWEROFF/BEEP/IMG WAV resource ids (table 0x1E494).
   * **0x6334 (state_startup_init)**:
     - 0x6358..0x6380: `while (byte 0x10FBC8 == 0 && ++n < 0x32) OSTimeDly(5)`: waits for the display task's init-done flag, **max 50 x 50 ms = 2.5 s**, polled every 50 ms (constants: 0x6360 `e3a00005` ticks, 0x637C `e3560032` count). Flag set by display task at 0x1669C after frame allocation, so this only blocks as long as LCD init takes (UNKNOWN time).
     - 0x7AA18(0x3A,0x14) (GPIO/pin config, INFERRED).
     - **0x62A8**: 0x310FC(0) (PMU status read, result unused), **OSTimeDly(10) = 100 ms unconditional (0x62B8 `e3a0000a`)**; if DC-in present (0x6F5C = 0xD0000068 bit2) and marker==0xA5 -> show BATTERY00.GPZP (0x61FC: OSTimeDly(1) loop until frame, OSTimeDly(10), speaker on, **OSTimeDly(150) = 1.5 s** 0x627C) then speaker off and hard power-off 0x30F00. Otherwise: if DC-in -> continue; if no DC-in: `while (power key pressed (0xD0000068 bit0)) { if OSTimeGet() >= 20 ticks (0x62C0 `e3a04014`) return; }` and if the key is released before OS tick 20 -> **power off** (short-press rejection, 200 ms after scheduler start).
     - 0x1F788 / 0x1E38C audio init; GPIO 0x5F (speaker amp enable, INFERRED from 0x286A8) = 0.
     - 0x63B4..0x63CC: if sound-started counter 0x10C3EE == 0: 0x1EA44(2) = **play POWERON_AUDIO.WAV** (returns 0 if settings beep +0x19 == 0 or audio task busy). Playback is asynchronous via the audio task; nothing waits for it to finish.
     - 0x61054 (unknown), then **POWER_ON_LOGO.JPG**: resource find 0x1510, malloc 0x639AC, load 0x1720, frame alloc 0x14698 (pool 0x10FCC0), JPEG decode 0x22AEC (blocking, duration UNKNOWN, 160x80x2 = 0x6400 bytes copied at 0x64C0 -> LCD is 160x80 RGB565, INFERRED), post frame to display queue 0x10FD5C with flag 0x10000.
     - free, **OSTimeDly(5) = 50 ms (0x64F8)**, 0x82DFC(1), speaker amp on 0x286A8(1), GPIO 0x26 = 1 (LED; 0x8664 blinks it while recording), 0x60BF8(0).
   * 0x2D51C(0xF8500000) (vector/irq related).
   * **Wait loop 0x65E8..0x6660** on queue 0x110D88: exits when (sound-started flag 0x10C3EE != 0 AND storage flag 0x10C3ED != 0) or msg 0x5025; 0x10C3ED is set by msgs **0x5013/0x5014** which the AP task's housekeeping 0x303C posts after SD detection: 0x3074.. 0xE3890, 0x26988(0), mount 0xE2E88(2) retried up to 3x (0x3098..0x30B0), "C:\DCIM"/"C:\SPIDCIM" creation 0xE3360/0xE2D40; 0x5013 = card ok, 0x5014 = no card. 0x303C runs when the AP task receives 0x5001, which 0x1F28 posts once immediately at AP init (0x1F84) and then every 1 s (0x27CAC slot 1 period 0x80). So the logo stays up at least until SD detection/mount completes (time UNKNOWN; absent card = fast fail path, no long timeout found: the mount retry is 3 immediate attempts).
   * After the loop: re-post the storage message to the queue for the next state, **0x7F10**: 0x7E28(1) key table init (key0 = power key = PMU bit0, callbacks 0x74A0/0x798C/0x7A34), 0x6D20 ADC init + register periodic msg 0x6000 (slot 4, period 4 ticks); **0x6578**: posts state msg **0x10D** (home screen) to 0x110D90 and 0x6006 to the peripheral task (enables the DC-unplug power-off check 0x7C18).
4. **Home/mode screen** state 0x2526C (entered via table index 0xD -> 0x259DC; VERIFIED): 0x14158, 0x7E28(1), posts 0x5006 to the AP task (DCIM scan "*.jpg", 0x2AA4), draws HOME_SMALL.GPZP for mode byte 0xFFC39 (0x24FF0), registers refresh timer 0x704F period 0x60 (0.75 s). It leaves only on msg **0x7001** (0x254E4: 0x7E28(0); switch mode: 1->0x102 video state 0x1D6FC, 2->0x101 photo state 0x1C644, 3->0x104 (0xAF80), 4->0x105, 5->0x10B, 6->0x10A; plays CLICK). Keys 0x7002/0x7005 = mode++, 0x7004 = mode--. Who posts 0x7001 at boot is **UNKNOWN**: posters are key-release callbacks (0x7958 for states 0x101/0x102/0x104; 0x7BD0 for state ids 0x10D/0x105 i.e. the home screen), the seconds countdown in the peripheral task (0x8924, needs 0x10CC59=1 which nothing sets statically), 0x22CC (AP msg 0x5003, only posted from the photo state) and 0x1B7C8 (another state). Most likely the release of the still-held power button, or a short press, is what moves the home screen into live view; the owner should check whether live view appears without touching a key (§6).
5. **Photo/capture state** 0x1C644 (msg 0x101; "state_video_record_entry" string at 0x1CB8C is also in this state, community photo-capture 0x1CE58 is a handler inside it): entry 0x1C680..0x1C6E0: clears 0x110240/0x10FBC5, 0xCE80(0x101) state id, 0xCFB8, 0x64C44(0) (CDSP effect off), 0x7E28(3 if wifi else 1), **0x1C614 -> 0x1BD80 preview start** (display queue post, 0x14EB0(1), 0x1F710, 0x2205C, 0x22088, 0x1F248 getter (+0x12), icon draws 0x21E14/0x21DA0, 0x1BD54(0)/(1)), 0x1F880(1). No OSTimeDly is reachable from 0x1BD80 within 5 call levels; the sensor/CDSP start is driven by other tasks and cannot be timed statically (UNKNOWN). "sensor_preview_init" (0x6C368) is reached only via 0x6E754 (a preview pipeline function with no static caller; function pointer at 0x6F980).
6. USB: no boot wait. The USB task 0x36B70 and 0x303C (0xE0848) poll; PC_CAM/U_DISK modes are entered on events. Wi-Fi: never initialised (§3). No fixed splash timer other than those listed. Timeouts found: 2.5 s display-ready poll, 3 mount retries, none for Wi-Fi.

Summary of fixed boot delays (VERIFIED): 100 ms (0x62B8) + 50 ms (0x64F8) + up to 2.5 s display-ready poll (usually ends early) + 20-tick power-key hold requirement (not a delay if the key is held) + JPEG decode + SD detect/mount + home-screen exit condition (UNKNOWN).

### Power-on sound and logo hold
* Sound: posted to the audio task; the startup loop only requires that playback *started*; nothing waits for completion. The audio task may still be playing when preview starts.
* Logo hold: no explicit timer. Logo is shown until the home state's HOME_SMALL draw (after storage detection) and then live view. At power-off the logo is shown again for 600 ms (below).

## 3. Power-off, auto-off, standby (question 3)

### 3.1 ap_state_handling_power_off (0x23008, VERIFIED; arg r6: 0 = normal, 1 = "USB/other")
0xCFA0; speaker amp on 0x286A8(1); post 0x600D (cancels countdown 0x10CD80+0x18); 0x21EFC; 0x21C1C (display toggle msg); if r6==0: 0x1EA44(3) **POWEROFF_AUDIO.WAV**; `OSTimeDly(0x14)` 200 ms (0x23050); if r6==0: re-display **POWER_ON_LOGO.JPG** (0x23070..0x23198) then `OSTimeDly(0x3C)` **600 ms (0x2319C)**; free; 0x1F760 (beep getter) -> OSTimeDly(1) or 0x1EB78; 0x6DA8; OSTimeDly(2); 0xE2EBC(2); 0x27210(0); 0x1FC74; GPIO 0x5F=0 (amp off); 0x33BC8; 0x2DF4C(0) (ADC off); if r6!=0: `OSTimeDly(10)` loop that never exits (0x2320C..0x23220: `and r4,r0,#0xff; cmp r4,#0x12c`), i.e. "not poweroff" idle-forever; wait <= 2 s for byte 0x10CD7C == 0 (0x23224..0x2324C, `cmp r4,#0xc8`); 0x2ED78(0,0,0); 0x2ECF4(0,0); 0x2EBA4(0x40,0); 0x362D8; GPIO 0x5F=0; 0x286A8(0); 0x28688; 0x32204(0..3) and 0x325C0(0..2) (timers off); wait until 0x310FC(0)==0 (power key released, 0x232CC..0x232D8); 0x2F3B8(0xF,0); 0x2F45C; 0x32CD8; tail-call **0x30F00**: 0x30EE8(0); 0x30D74(3) -> 0xD0000028[2:0]=3; 0x30D54(1) -> 0xD0000028 bit15; `b .` forever (PMU off, VERIFIED register writes, meaning of mode 3 INFERRED).
Settings are written back when dirty by 0x1FBC4 (called from the states on msg 0x7017 every 5 s and in several exits), not by the power-off routine itself.

Other paths to 0x30F00: 0x62A8 (boot short-press / low-battery), 0x8470 (power key loop in peripheral code, no callers found).

### 3.2 Idle auto-off (VERIFIED code, in the key scanner 0x7584 tail, run every 0x6000 period = 31.25 ms INFERRED)
```
0076fc  ldrb r0,[sl,#3] ; ldrb r1,[sl,#0x19]      ; sl = 0x10CC58 peripheral struct
007708  cmp r0,r1 ; strne r8(0),[sl,#0x70] ; strbne r0,[sl,#0x19]   ; busy-state change resets idle
007714  bl 0x1f7b0            ; r5 = settings byte +0x01  (auto-off index)
00771c  bl 0x1f4a0            ; r0 = settings byte +0xe7
007724  cmp r5,#0 ; beq 0x77d0            ; 0 = auto-off disabled
00772c  [sl+3] != 0 or [sl+0xd] != 0 -> skip (busy / USB)
007740  bl 0x7488 ; ([0x10cc58+0x68]==2) -> skip (USB storage mode)
00774c  cmp r5,#1 ; moveq r5,#3 ; beq 0x7760      ; index 1 -> 3 min
007758  cmp r5,#2 ; moveq r5,#5                  ; index 2 -> 5 min   (file 0x775C: 03 a0 05 05 -> e3? see bytes: 0550a003)
007760  ldr r0,[sl,#0x70] ; rsb r1,r5,r5,lsl#4   ; r1 = 15*M
007768  add r0,r0,#4 ; str ; lsr r0,r0,#7         ; idle_seconds = ticks/128, +4 per 31.25 ms run
007774  cmp r0,r1,lsl#2 ; bls 0x77cc              ; expire when idle_seconds > 60*M
00777c  str r8,[sl,#0x70] ; ... post 0x703d, [fp]=7, post 0x7006 (power off) to state queue 0x110D88
```
Idle reset: any key press (0x767C `str r8,[sl,#0x70]`), 0x6DC0 (called from 0x8BBC on msg 0x6008), init 0x6D20.
Current settings record: byte +0x01 = 2 -> **5 minutes** (if the 128 Hz tick inference holds; 3 min would need an unlikely tick rate). The quick-start guide's "3 minutes" matches index 1. Also: auto-off is not applied while recording ([0x10CC58+3]) or in USB modes.

### 3.3 Second idle stage ("standby", VERIFIED code, INFERRED meaning)
0x77CC..0x7868: uses settings byte +0x51 (getter 0x1F7D8), threshold 60*value seconds (no mapping); when expired: `0x23348(0)` (= post 0x6008 data 0 -> 0x6EA4 -> speaker amp off, VERIFIED), audio-stop msg 0x10000104 to the audio task if state 0x10B, and msg 0x600F -> peripheral sets flag 0x10CD82 = 1. While that flag is set a power-key press posts 0x703D (wake) instead of 0x7063 (0x74A0..0x74D4), and card changes re-enable (0x303C: 0x23348(1)). Current record +0x51 = 2 -> 2 minutes. No LCD-backlight register write was found in this path; whether the LCD is actually blanked is UNKNOWN (the display toggle 0x21C1C posts 0x09FFFFFF to the display queue in other countdowns).

### 3.4 Other power-off triggers (VERIFIED)
* Power key held >= 48 runs (1.5 s): 0x74E8 -> 0x7006.
* Battery level 0 for > 4 stable samples: 0x7254..0x728C -> 0x7006 and marker 0x83=0xA5 (next boot shows BATTERY00 then powers off unless charging).
* DC-in removed while in mode 2 (0x10CC58+0xC == 2, "charge-only"): 0x7C18 -> 0x7006 ("Use after unplugging the power").
* malloc(1500) failure in 0x720FC -> 0x7006 forever; menu item (0x799D4); SD failure path 0x262C8.
* Countdown 0x10CD80+0x18 = 0x51F (msg 0x600C, 186 runs with a 1 Hz display toggle, then 0x7006): no sender of 0x600C found -> dead.
* 0x6003 handler (0x8B80): if byte 0x10CD81 != 0 the peripheral task sleeps **3000 ticks = 30 s** (0x8B8C) then posts 0x502B to the AP task (INFERRED: delayed USB/charge handling).

### 3.5 Settings store (VERIFIED)
* Flash 0x170000: append-only records of 0x100 bytes = 0xFC data + CRC32 (zlib/IEEE, little-endian) at +0xFC; 16 valid records at 0x170000..0x171E00 in this dump (all with auto-off=2, standby=2). 0x16E000 holds a 'JHCM' marker area written by 0x73DA0 (all 0xFF here).
* RAM copy at VA 0x1107DC (0xFC bytes + CRC), loaded by 0x1FD24, dirty flag +0xFA, defaults 0x1F89C. Fields identified: +0x01 auto-off index (default 2), +0x51 standby index (default 2), +0x19 sound on/volume (default 5, used as boolean by 0x1EA44), +0x50 (default 2 then 0), +0x12 resolution-like (many users), +0xE6 (default 1), +0x0F (default 6), +0x29 (default 4). The settings menu state 0x77EAC reads items via the getter switch at 0x78EA4..0x78F38 (+0x50,+0x15,+0xE7,+0x29,+0xE6,+0x12,+0x0F,+0xE5,+0x0B,+0x14,+0x11,?,+0x35,+0x52,+0xE9,+0x28,+0x53). Generic SDK strings "Auto Off"/"LCD Off"/"1 Min." (0x141BDC..) are in the resource pack and are not referenced by code; the Charmera UI uses the index->minutes mapping above.

## 4. Clocks (question 4)

* Header: 0xD0000028 = 0x8005 (bit15 + mode 5) and 0xD00000AC = 0x104 after the unlock keys (§0). Meaning of mode bits UNKNOWN (datasheet).
* main -> 0x36274 -> 0x312FC: **sysclk_hz = 13,500,000 + 4,500,000 * (0xD000005C & 0x3F)** (0x31274, VERIFIED formula), stored at VA 0x11F7D4; then 0x30FB8(0,4): 0xD00000AC = (old & ~0xF) | 4; 0x31084(1): 0xD0000020 bits 6:4 = 1. Then 0x326F8 installs exception vectors at 0xF8400000, 0x3661C/0x345D4/0x33A68/0x33C40/0x2E404(0)/0x31704 (interrupt controller, DMA, GPIO, timebase init; INFERRED).
* The PLL multiplier field 0xD000005C[5:0] is not written by the header and the only code that sets it (0x30D94, f = 40 + 4N MHz mapping, max N=0x1A) has **no callers**; writes at 0x301AC/0x301D4 only touch bits 14:9 (SPI clock divider = 2*sysclk/36 MHz - 1, i.e. **SPI NOR at 36 MHz**, 0x1B8C) and 0x2C7C4 sets bit 8. So the CPU clock is whatever the boot ROM/reset default leaves in 0xD000005C: **UNKNOWN statically**. The time-base conversion 0x31CD4 special-cases 24, 48, 78 and 144 MHz, so 144 MHz is the likely nominal value (INFERRED, weak).
* Peripheral clock enables: 0x2C794 writes 0xD0000010 = 0xFE7F, 0xD0000014 = 0xCF5D, 0xD00000E0 = 0x19DE and clears bit 28 of 0xD000000C (in the CDSP/sensor init chain 0x2C670 <- 0x2BD24 <- ... <- 0x5F7B0).
* Dynamic clock changes: none at idle. 0xD0000028 is only changed in power-off (mode 3) and in state 0x21370 (msg 0x108, USB mass-storage; 0x213A4 sets bit15 and 0x21548 resets the SPI divider). The uC/OS-II idle task body / idle hook was not located (UNKNOWN); ARM7TDMI has no WFI, and no halt-register write other than the power-off sequence was found.

## 5. Battery and charging (question 5)

* ADC: 0xC00D0000 (trigger/busy at +4, data at +8). Runtime sampling: peripheral run 0x72EC every >= 2 OS ticks, channel 1 (0x2E020(1)), result delivered by the ADC callback 0x6DB4 into halfword 0x10CC58+0x32 = (adc12 << 4) | channel. A battery sample is accepted every 33 runs (~1 s); samples must be stable (5 % change rule 0x7370..0x73B0) twice.
* Level: 0x6F74: rolling 5-slot window at 0x10CCCC, sum of 4 samples compared with 4*threshold, thresholds table **file 0xF3864 = {0, 2190, 2420, 2600} ADC counts** -> level 0..3 (0: avg <= 2190, 1: <= 2420, 2: <= 2600, 3: above), hysteresis 20/4 = 5 counts (0x7038 `mov r3,#0x14`, 0x7070 `mvn r3,#0x13`), stricter direction when charging. Level stored at 0xFF948+5/+7, UI redraw msg 0x703E with pointer 0xFF94F when level < 4 (BATTERY00..03 icons, INFERRED).
* Low battery: level 0 -> first time msg 0x7066 (warning), after > 4 consecutive stable samples -> 0x7006 power off and marker 0x83 = 0xA5 (0x7260..0x728C). Boot check 0x6104 uses a raw read with window 0x47F..0x5A9 (different scaling, UNKNOWN relation), then 0x62A8 shows BATTERY00 for 1.5 s and powers off unless DC-in.
* ADC-to-volts: divider ratio and ADC reference are UNKNOWN; thresholds are raw counts.
* Charging/DC detect: PMU status 0xD0000068 bit2 (0x6F5C); bit0 = power key (0x310FC(0)); bit1 = 0x310FC(1) (unused in the paths read). 0x7098 posts 0x703F on DC connect and 0x7040 on disconnect, keeps a charging flag 0x10CC58+0x15; 0x7C18 debounces DC state over 16 runs and powers off on unplug when in charge-only mode. No charger IC register access found (charging is hardware, INFERRED).

## 6. Patch candidates (question 6) and measurements

All are single-word changes unless noted; file offsets; current bytes little-endian. Risks are static guesses.

| # | file off | current | meaning | change | risk |
|---|---|---|---|---|---|
| 1 | 0x62B8 | `0a 00 a0 e3` (mov r0,#10) | 100 ms OSTimeDly before power-key/DC check | #2 (20 ms) | low-medium: acts as PMU/ADC settle+debounce |
| 2 | 0x64F8 | `05 00 a0 e3` | 50 ms after posting the logo frame (frame is freed right after) | #1 or #2 | medium: display task must consume the frame first |
| 3 | 0x6360 | `05 00 a0 e3` | display-ready poll interval (x50) | #1 | low (saves <= 40 ms) |
| 4 | 0x63BC | `a0 61 00 eb` (bl 0x1EA44) | play POWERON WAV | `00 00 a0 e1` (nop) or set settings +0x19 = 0 | low; frees audio task during boot; loop still exits (flag set by the 0x63CC path only if r0!=0 -> **also nop 0x63C0..0x63CC or keep**: simplest is settings +0x19=0 which also silences every sound) |
| 5 | 0x63D0..0x64E4 | logo decode/display | skip by patching 0x63D8 `bl 0x1510`'s result path; easier: replace 0x63D4 `add r0,pc,#0x168` target string with a non-existent name so 0x1510 returns 0xFFFF and the code jumps to 0x64E8 | removes JPEG decode (tens of ms, UNKNOWN) | low-medium (resource name is data: 0x6544 "POWER_ON_LOGO.JPG" -> e.g. "POWER_ON_LOGO.JP_" ) |
| 6 | 0x6714 | `0d 01 00 00` (literal 0x10D used by 0x6584) | first state after startup = home screen | `01 01 00 00` -> enter photo state directly | medium-high: the home state also kicks the DCIM scan (msg 0x5006) and key setup; only worth it if the owner confirms the home screen waits (see measurements) |
| 7 | 0x775C | `05 50 a0 03` (moveq r5,#5) | auto-off minutes for setting index 2 | #3 -> 3 min, #1 -> 1 min | low; or change settings byte +0x01 in the record (needs CRC32 update over 0xFC bytes, and every valid record is read in sequence; safest to append a new record) |
| 8 | record +0x51 | 2 | standby stage after 2 min | 1 | low (CRC) |
| 9 | 0x2319C | `3c 00 a0 e3` | 600 ms logo hold at power-off | #1 | cosmetic, low |
| 10 | 0x23050 | `14 00 a0 e3` | 200 ms after POWEROFF sound | #1 | low (cuts the sound) |
| 11 | 0x627C | `96 00 a0 e3` | 1.5 s BATTERY00 hold at low-battery boot | - | leave |
| 12 | 0x6D84 | `04 30 a0 e3` | peripheral period 4 ticks (31.25 ms) | - | **do not change**: idle seconds math and key timings assume it |
| 13 | 0x1F94 | `80 30 a0 e3` | AP housekeeping 1 s | - | leave |

Battery-life levers: auto-off/standby settings (#7, #8); the speaker amp GPIO 0x5F stays enabled after boot (0x286A8(1) at 0x651C) - turning it off until a sound plays saves the amp's quiescent current (0x1EA44/0x1EB0C would need to enable it; code change, medium); LED GPIO 0x26 is driven high at 0x6528 and kept high outside recording (0x8664) - INFERRED status LED, data-free patch: 0x6520 `mov r1,#1` -> `mov r1,#0` (file 0x6520 `01 10 a0 e3` -> `00 10 a0 e3`), low risk if it is only an LED (UNKNOWN). The Wi-Fi stack is never powered (§7), so nothing to gain there. No idle clock scaling exists; adding one is a code change beyond "low risk".

Measurements the owner should make first (stock):
1. Stopwatch: power press -> logo appears; logo -> HOME icon (if visible) -> live view; with and without SD card; does live view appear without touching a key (resolves the 0x7001 question and whether #6 helps)?
2. Minimum power-key hold time (expect ~200 ms + boot ROM time).
3. Auto-off: time from last key press to power-off at the default setting (expect 5:00 if the 128 Hz tick holds; 3:00 would mean the tick is ~77 Hz and all §3 times scale by 0.6) and whether something changes at 2:00 (standby stage: speaker off, screen?).
4. Idle current at live view, after standby, and off; current with the LED masked (tape) to see if GPIO 0x26 matters.
5. Whether the power-on sound is cut short when live view starts.

## 7. Wi-Fi stack (question 2)

VERIFIED static conclusion: **never initialised**.
* `ssv6xxx_wifi_init` (string 0xA22AC, function 0xA20A4) is called only from 0x4659C inside 0x463B0 (`[33m SSV dev init %d, DevInit=%d` message, retry loop 0x4659C..0x465BC: on failure prints, 0xACC00, 0xA54B0, loops back while 0xA54B0 returns 0; delays inside 0x46F9C: OSTimeDly(10) at 0x46FBC and OSTimeDly(30) at 0x46FCC).
* 0x463B0 is reached from (a) 0x46F9C = Wifi_mode_active (<- 0x60D58 "Wifi_mode_active" <- 0x60F3C mode switch with r0==1) and (b) 0x62C38 <- 0x62E24 <- 0x62F54 <- 0x46C94, which has no callers and no function-pointer references.
* 0x60F3C has 9 call sites (0xB250, 0x1CBF8, 0x1CD7C, 0x1CE08, 0x1CF54, 0x1DBB4, 0x1E244, 0x1E284 and 0xB654/0xB978 conditional) and **every one passes r0 = 0 (shutdown)** under `if (wifi_active_flag 0x140724)`; no `b` tail calls and no pointer references to 0x60F3C/0x60D58/0x46F9C exist. netdev_init (0x39578) is reachable only from the netmgr code (0x9CDA4 <- 0x9EFF4/0x9FF34 <- 0xA03E8, referenced only from a pointer at 0x1095D0 in the netmgr tables). No task entry among the 7 boot tasks belongs to the Wi-Fi driver. Hence no retry, no timeout, no power spent on Wi-Fi at boot or idle.

## 8. Open questions
* What posts 0x7001 to leave the home/mode screen after boot (key release vs. automatic).
* Actual hardware timer-2 rate (1/128 s inferred from 7680 ticks per minute and 230272 = 1799 s).
* CPU/PLL frequency (0xD000005C[5:0] not set by firmware); SDRAM init location (boot ROM?).
* Sensor and LCD init durations (other tasks, no OS delays found in the preview-start closure).
* Whether the standby stage blanks the LCD (no backlight register write found).
* Identity of GPIO 0x26 (LED assumed) and GPIO 0x5F (speaker amp assumed), and the ADC divider ratio.

## 9. Key function table (file offsets)
0x23C reset; 0x2DD48 main; 0x2DCAC root task; 0x9012C OSTaskCreate; 0x906E4 OSTimeDly; 0x90734 OSTimeDlyHMSM; 0x908B8 OSTimeGet; 0x8F87C OSQPost; 0x25B84 msgQSend wrapper; 0x25C38 msgQReceive wrapper; 0x27CAC timer register; 0x27DA0 timer cancel; 0x32504 hw timer start; 0x36104 gpio_set(pin=(port<<4)|bit, val) regs 0xC0000000+port*0x20; 0x2E96C/0x2EBA4 pin/reg read/write by id; 0x310FC PMU status bit (0xD0000068); 0x30F00 final power off; 0x23008 ap_state_handling_power_off; 0x23348 speaker mute/unmute via 0x6008 (not "state set"); 0x286A8 speaker amp enable; 0x1EA44 play WAV by index (0 CLICK? table: 0->+4 CLICK, 2->POWERON, 3->POWEROFF, 5->BEEP, 6->IMG); 0x1510 resource lookup by name; 0x22AEC JPEG decode; 0x65B0 state_startup; 0x6334 state_startup_init; 0x62A8 boot key/battery check; 0x6104 boot ADC; 0x303C AP housekeeping (SD); 0x2526C home/mode state; 0x1C644 photo/capture state; 0x1D6FC video state; 0xAF80 state 0x104; 0x21370 state 0x108 (USB); 0x77EAC settings menu state; 0x8758 peripheral task; 0x886C periodic handler (0x6000); 0x7584 key scanner + auto-off; 0x72EC ADC sampling; 0x7098/0x6F74 battery level; 0x7C18 DC unplug; 0x1FD24/0x1FAA8 settings load; 0x1F89C defaults; 0x1FBC4 settings save-if-dirty; 0x31274 sysclk formula; 0x3017C SPI clock divider; 0xDF2F4 PLL unlock keys; 0x60F3C Wi-Fi mode switch; 0x46F9C Wifi_mode_active; 0xA20A4 ssv6xxx_wifi_init.
