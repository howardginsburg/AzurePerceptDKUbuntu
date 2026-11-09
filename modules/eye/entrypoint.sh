#!/bin/bash
# Starts the inference app with the options set by EYE_* environment variables (config/percept.env).
# Unset or empty variables leave the app's defaults; arguments given to the container are appended.
#   EYE_MODEL        model: /config/files/<file> (.zip, .blob, .xml with its .bin, .onnx) or an http(s) URL to a .zip
#   EYE_PARSER       ssd100, ssd200, yolo, classification, s1, openpose, onnxssd, faster-rcnn-resnet50, unet, ocr
#   EYE_LABELS       label file, one class name per line
#   EYE_RESOLUTION   native, 1080p or 720p
#   EYE_FPS          output video frame rate
#   EYE_TIME_ALIGN   1 = delay the video so boxes line up exactly with their frame
set -e
args=()
if [ -n "${EYE_MODEL:-}" ]; then args+=("-m=$EYE_MODEL"); fi
if [ -n "${EYE_PARSER:-}" ]; then args+=("-p=$EYE_PARSER"); fi
if [ -n "${EYE_LABELS:-}" ]; then args+=("-l=$EYE_LABELS"); fi
if [ -n "${EYE_RESOLUTION:-}" ]; then args+=("-s=$EYE_RESOLUTION"); fi
if [ -n "${EYE_FPS:-}" ]; then args+=("--fps=$EYE_FPS"); fi
case ${EYE_TIME_ALIGN:-0} in 1|true|yes|on) args+=("-t") ;; esac
exec /app/inference "${args[@]}" "$@"
