LAZ compressor for the viewer: laz-perf (https://github.com/hobuinc/laz-perf, Apache-2.0)
compiled to WebAssembly with Zig. wrap.cpp exposes a tiny C API (compress chunks of LAS
point-format-2 records, build the chunk table and the LASzip VLR); the JavaScript in
docs/index.html writes the LAS 1.2 header around it. Output was verified with laspy + lazrs.
