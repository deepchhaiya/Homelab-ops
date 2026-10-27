#!/bin/bash
# Clear Chromium's profile lock at container start. Nothing can be running yet, so any
# lock left in /config is stale (Chromium died with Xvfb, or the container was recreated).
#
# The image's /usr/bin/chromium wrapper tries this itself with `if ! pgrep chromium`, but
# that pgrep matches the wrapper script (also named chromium), so the cleanup never runs.
# A lock naming another hostname then makes Chromium exit with code 21 ("profile appears
# to be in use ... on another computer"), and the image has no zenity/xmessage to show
# that, so the browser just never opens.
rm -f /config/.config/chromium/SingletonLock \
      /config/.config/chromium/SingletonCookie \
      /config/.config/chromium/SingletonSocket
echo "[chromium-unlock] cleared stale Chromium profile lock"
