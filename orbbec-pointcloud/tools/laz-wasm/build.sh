#!/bin/sh
# Rebuilds lazenc.wasm (the LAZ compressor embedded in docs/index.html as base64).
# Needs: pip install ziglang ; git clone https://github.com/hobuinc/laz-perf.git
set -e
SRC=${LAZPERF:-laz-perf/cpp}
python3 -m ziglang c++ -target wasm32-wasi -Os -s -fno-exceptions -mexec-model=reactor -std=c++17 \
  -D__EMSCRIPTEN__ -I"$SRC" -include nothrow.h -DLAZPERF_EXPORT= \
  wrap.cpp "$SRC"/lazperf/lazperf.cpp "$SRC"/lazperf/vlr.cpp "$SRC"/lazperf/charbuf.cpp "$SRC"/lazperf/detail/*.cpp \
  -o lazenc.wasm
base64 -w0 lazenc.wasm > lazenc.b64
echo "Paste the contents of lazenc.b64 into window.LAZ_WASM_B64 in docs/index.html"
