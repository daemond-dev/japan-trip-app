#!/bin/sh
# Build the Android app. Usage: ./build.sh <versionName> [versionCode]   (run from the android/ folder)
# Needs: Android SDK (platforms;android-35, build-tools;35.0.0), Java 17+, ecj.jar, python3. See README.md.
# Settings come from local.properties (copy local.properties.example). The page and data are taken from ../app and ../data.
set -e
VER=${1:?usage: build.sh <versionName> [versionCode]}; CODE=${2:-$(date +%s | cut -c1-9)}
[ -f local.properties ] || { echo "Copy local.properties.example to local.properties and fill it in."; exit 1; }
getp(){ sed -n "s/^$1=\([^#]*\).*/\1/p" local.properties | sed 's/[[:space:]]*$//'; }
APP_ID=$(getp APP_ID); APP_NAME=$(getp APP_NAME); LIVE_URL=$(getp LIVE_URL); KEYSTORE=$(getp KEYSTORE); KS_ALIAS=$(getp KS_ALIAS); KS_PASS=$(getp KS_PASS)
[ -n "$APP_ID" ] && [ -n "$LIVE_URL" ] && [ -n "$APP_NAME" ] || { echo "APP_ID, APP_NAME and LIVE_URL must be set in local.properties"; exit 1; }
case "$LIVE_URL" in *XXXXXXXX*) echo "LIVE_URL in local.properties is still the placeholder"; exit 1;; esac
SDK=${SDK:-/opt/android/sdk}; BT=$SDK/build-tools/35.0.0; AJ=$SDK/platforms/android-35/android.jar; ECJ=${ECJ:-/tmp/ecj.jar}
PKGPATH=$(echo "$APP_ID" | tr . /)
rm -rf out && mkdir -p out/res out/gen out/classes "out/src/$PKGPATH" assets/www
# generated inputs: the page as the offline copy, the data bundle
python3 ../scripts/patch_html.py ../app/index.html assets/www/index.html
python3 ../scripts/make_data.py ../data assets/www/data.js
# templated sources
sed -e "s/__APP_ID__/$APP_ID/" -e "s/__VERSION_CODE__/$CODE/" -e "s/__VERSION_NAME__/$VER/" AndroidManifest.xml > out/AndroidManifest.xml
sed -e "s#__APP_ID__#$APP_ID#" -e "s#__LIVE_URL__#$LIVE_URL#" src/MainActivity.java > "out/src/$PKGPATH/MainActivity.java"
cp -r res out/res-src && sed -i "s#__APP_NAME__#$APP_NAME#" out/res-src/values/strings.xml
[ -f "$KEYSTORE" ] || { echo "No $KEYSTORE yet: creating one. Keep it; updates must be signed with the same key."; keytool -genkeypair -keystore "$KEYSTORE" -alias "$KS_ALIAS" -keyalg RSA -keysize 2048 -validity 10000 -storepass "$KS_PASS" -keypass "$KS_PASS" -dname "CN=$APP_NAME, O=$APP_ID, C=US"; }
$BT/aapt2 compile --dir out/res-src -o out/res/res.zip
$BT/aapt2 link -I $AJ --manifest out/AndroidManifest.xml -o out/base.apk --java out/gen -A assets out/res/res.zip --auto-add-overlay
java -jar $ECJ -source 1.8 -target 1.8 -nowarn -bootclasspath $AJ -cp $BT/core-lambda-stubs.jar -d out/classes "out/src/$PKGPATH/MainActivity.java" "out/gen/$PKGPATH/R.java"
$BT/d8 --min-api 24 --lib $AJ --output out $(find out/classes -name '*.class')
cp out/base.apk out/unsigned.apk && (cd out && zip -q -j unsigned.apk classes.dex)
$BT/zipalign -f -p 4 out/unsigned.apk out/aligned.apk
NAME=$(echo "$APP_NAME" | tr -d ' ')
$BT/apksigner sign --ks "$KEYSTORE" --ks-key-alias "$KS_ALIAS" --ks-pass "pass:$KS_PASS" --key-pass "pass:$KS_PASS" --out "out/$NAME-$VER.apk" out/aligned.apk
$BT/apksigner verify "out/$NAME-$VER.apk" && echo "built out/$NAME-$VER.apk (versionCode $CODE)"
