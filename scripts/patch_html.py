"""Turn the live app page (Artifact read, or the app/page db doc) into the Android app's built-in offline copy.
The page already contains its Android behavior (it checks window.__APK); this only adds the offline data and
runtime stand-in right after <head> (older pages had web-font links there, which are swapped out if present).
Usage: python3 patch_html.py live_index.html assets/www/index.html"""
import sys
s = open(sys.argv[1], encoding='utf-8').read()
inject = '<script src="data.js"></script>\n<script src="shim.js"></script>\n'
fonts = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
         '<link href="https://fonts.googleapis.com/css2?family=Barlow+Semi+Condensed:wght@500;600;700&family=Zen+Kaku+Gothic+New:wght@400;500;700&display=swap" rel="stylesheet">\n')
if s.count(fonts) == 1:
    s = s.replace(fonts, inject)
elif '<head>' in s:
    s = s.replace('<head>', '<head>\n' + inject, 1)   # the first <head> is the page's own (the offline-copy template inside the script has one too)
else:
    sys.exit("no <head> found")
open(sys.argv[2], 'w', encoding='utf-8').write(s)
print("patched")
