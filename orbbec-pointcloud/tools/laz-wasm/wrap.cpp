#include "lazperf/lazperf.hpp"
#include "lazperf/vlr.hpp"
#include <vector>
#include <cstdlib>
static std::vector<unsigned char> out;
static std::vector<char> vlrbuf;
extern "C" {
__attribute__((export_name("lz_malloc"))) void* lz_malloc(size_t n) { return malloc(n); }
__attribute__((export_name("lz_free"))) void lz_free(void* p) { free(p); }
__attribute__((export_name("lz_reset"))) void lz_reset() { out.clear(); out.shrink_to_fit(); }
// Compress `count` LAS point-format-2 records (26 bytes each) as one LAZ chunk; returns the chunk's byte size.
__attribute__((export_name("lz_chunk"))) size_t lz_chunk(const char* pts, int count) {
  size_t before = out.size();
  lazperf::point_compressor_2 c([](const unsigned char* b, size_t n) { out.insert(out.end(), b, b + n); }, 0);
  for (int i = 0; i < count; i++) c.compress(pts + (size_t)i * 26);
  c.done();
  return out.size() - before;
}
__attribute__((export_name("lz_out_ptr"))) const unsigned char* lz_out_ptr() { return out.data(); }
__attribute__((export_name("lz_out_len"))) size_t lz_out_len() { return out.size(); }
static std::vector<unsigned char> tbl;
// Compressed chunk table for fixed-size chunks; sizes = byte length of each chunk.
__attribute__((export_name("lz_table"))) size_t lz_table(const uint32_t* sizes, int n) {
  std::vector<lazperf::chunk> chunks;
  for (int i = 0; i < n; i++) chunks.push_back({0, sizes[i]});
  tbl.clear();
  lazperf::compress_chunk_table([](const unsigned char* b, size_t k) { tbl.insert(tbl.end(), b, b + k); }, chunks, false);
  return tbl.size();
}
__attribute__((export_name("lz_table_ptr"))) const unsigned char* lz_table_ptr() { return tbl.data(); }
__attribute__((export_name("lz_vlr"))) size_t lz_vlr(int format, unsigned chunk) {
  vlrbuf = lazperf::laz_vlr(format, 0, chunk).data(); return vlrbuf.size();
}
__attribute__((export_name("lz_vlr_ptr"))) const char* lz_vlr_ptr() { return vlrbuf.data(); }
}
