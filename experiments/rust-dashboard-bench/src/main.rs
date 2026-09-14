//! Persistent stdin/stdout JPEG encoder.  Requires the Linux system libturbojpeg.
use std::{
    ffi::CStr,
    io::{self, Read, Write},
    os::raw::{c_char, c_int, c_ulong, c_void},
    ptr, slice,
};

const WIDTH: i32 = 1_600;
const HEIGHT: i32 = 720;
const QUALITY: c_int = 85;
const MAX_INPUT: usize = 1_600 * 720 * 4;
const MAX_OUTPUT: usize = 16 * 1024 * 1024;

// These orderings mirror enum TJPF and enum TJSAMP in /usr/include/turbojpeg.h
// for the installed libturbojpeg 3.2 ABI.  No version-specific TJFLAG is used.
#[allow(dead_code)]
#[repr(i32)]
enum PixelFormat { Rgb, Bgr, Rgbx, Bgrx, Xbgr, Xrgb, Gray, Rgba }
#[allow(dead_code)]
#[derive(Clone, Copy)]
#[repr(i32)]
enum Subsampling { S444, S422, S420 }
impl Subsampling {
    fn parse(value: &str) -> Result<Self, String> {
        match value { "420" => Ok(Self::S420), "444" => Ok(Self::S444), _ => Err("subsampling must be 420 or 444".into()) }
    }
}

type TjHandle = *mut c_void;
#[link(name = "turbojpeg")]
unsafe extern "C" {
    fn tjInitCompress() -> TjHandle;
    fn tjCompress2(handle: TjHandle, src: *const u8, width: c_int, pitch: c_int, height: c_int,
                   pixel_format: c_int, jpeg: *mut *mut u8, jpeg_size: *mut c_ulong,
                   subsampling: c_int, quality: c_int, flags: c_int) -> c_int;
    fn tjBufSize(width: c_int, height: c_int, subsampling: c_int) -> c_ulong;
    fn tjFree(buffer: *mut u8);
    fn tjDestroy(handle: TjHandle) -> c_int;
    fn tjGetErrorStr() -> *mut c_char;
}

struct ForeignBuffer(*mut u8);
impl Drop for ForeignBuffer { fn drop(&mut self) { if !self.0.is_null() { unsafe { tjFree(self.0) } } } }
struct Encoder { handle: TjHandle, output: Vec<u8>, output_cap: usize, subsampling: Subsampling }
impl Drop for Encoder { fn drop(&mut self) { if !self.handle.is_null() { unsafe { tjDestroy(self.handle); } } } }

impl Encoder {
    fn new(subsampling: Subsampling) -> Result<Self, String> {
        let output_cap = usize::try_from(unsafe { tjBufSize(WIDTH, HEIGHT, subsampling as c_int) })
            .map_err(|_| "tjBufSize does not fit Rust usize")?;
        if output_cap == 0 || output_cap > MAX_OUTPUT { return Err("invalid tjBufSize output bound".into()); }
        let handle = unsafe { tjInitCompress() };
        if handle.is_null() { return Err(turbo_error("tjInitCompress failed")); }
        Ok(Self { handle, output: Vec::with_capacity(output_cap), output_cap, subsampling })
    }

    fn encode(&mut self, rgba: &[u8]) -> Result<&[u8], String> {
        if rgba.len() != MAX_INPUT { return Err("invalid RGBA payload length".into()); }
        let mut jpeg = ptr::null_mut();
        let mut jpeg_size: c_ulong = 0; // C API specifies unsigned long, not size_t.
        let status = unsafe { tjCompress2(self.handle, rgba.as_ptr(), WIDTH, WIDTH * 4, HEIGHT,
            PixelFormat::Rgba as c_int, &mut jpeg, &mut jpeg_size, self.subsampling as c_int, QUALITY, 0) };
        let foreign = ForeignBuffer(jpeg);
        if status != 0 { return Err(turbo_error("tjCompress2 failed")); }
        let used = usize::try_from(jpeg_size).map_err(|_| "JPEG size does not fit Rust usize")?;
        if foreign.0.is_null() || used == 0 || used > self.output_cap || used > MAX_OUTPUT { return Err("invalid JPEG output bound".into()); }
        let encoded = unsafe { slice::from_raw_parts(foreign.0, used) };
        self.output.clear();
        self.output.extend_from_slice(encoded); // Never give Vec ownership of TurboJPEG memory.
        Ok(&self.output)
    }
}

fn turbo_error(prefix: &str) -> String {
    let error = unsafe { tjGetErrorStr() };
    if error.is_null() { format!("{prefix}: TurboJPEG gave no error string") }
    else { format!("{prefix}: {}", unsafe { CStr::from_ptr(error) }.to_string_lossy()) }
}

fn read_exact_or_eof<R: Read>(reader: &mut R, buffer: &mut [u8], what: &str, eof_ok: bool) -> Result<bool, String> {
    let mut offset = 0;
    while offset < buffer.len() {
        match reader.read(&mut buffer[offset..]) {
            Ok(0) if offset == 0 && eof_ok => return Ok(false),
            Ok(0) => return Err(format!("truncated {what}")),
            Ok(count) => offset += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(format!("could not read {what}: {error}")),
        }
    }
    Ok(true)
}

fn read_frame<R: Read>(reader: &mut R, input: &mut [u8]) -> Result<Option<usize>, String> {
    let mut header = [0; 4];
    if !read_exact_or_eof(reader, &mut header, "frame header", true)? { return Ok(None); }
    let length = u32::from_be_bytes(header) as usize;
    if length > MAX_INPUT { return Err(format!("frame length exceeds {MAX_INPUT} bytes")); }
    if length != MAX_INPUT { return Err("invalid RGBA payload length".into()); }
    read_exact_or_eof(reader, &mut input[..length], "frame payload", false)?;
    Ok(Some(length))
}

fn usage() -> &'static str {
    "rust-dashboard-jpeg [--subsampling 420|444]\n\nPersistent 1600x720 RGBA -> JPEG quality 85 encoder. Requires the host Linux libturbojpeg.\nProtocol: big-endian u32 length + 4608000 RGBA bytes in; u32 + JPEG bytes out."
}
fn options() -> Result<Option<Subsampling>, String> {
    let mut args = std::env::args().skip(1);
    let mut subsampling = Subsampling::S420;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--help" | "-h" => { println!("{}", usage()); return Ok(None); }
            "--version" => { println!("rust-dashboard-jpeg 0.1.0"); return Ok(None); }
            "--subsampling" => subsampling = Subsampling::parse(&args.next().ok_or("--subsampling requires 420 or 444")?)?,
            _ => return Err(format!("unknown option: {arg}")),
        }
    }
    Ok(Some(subsampling))
}
fn run() -> Result<(), String> {
    let Some(subsampling) = options()? else { return Ok(()); };
    let mut encoder = Encoder::new(subsampling)?;
    let mut input = vec![0; MAX_INPUT]; // Fixed maximum: headers never drive allocation.
    let stdin = io::stdin(); let stdout = io::stdout();
    let mut reader = stdin.lock(); let mut writer = stdout.lock();
    while let Some(length) = read_frame(&mut reader, &mut input)? {
        let jpeg = encoder.encode(&input[..length])?;
        writer.write_all(&(jpeg.len() as u32).to_be_bytes()).map_err(|e| e.to_string())?;
        writer.write_all(jpeg).map_err(|e| e.to_string())?;
        writer.flush().map_err(|e| e.to_string())?;
    }
    Ok(())
}
fn main() { if let Err(error) = run() { eprintln!("rust-dashboard-jpeg: {error}"); std::process::exit(1); } }

#[cfg(test)]
mod tests {
    use super::*; use std::io::Cursor;
    #[test] fn clean_eof_is_distinct_from_a_partial_header() {
        let mut input = vec![0; MAX_INPUT];
        assert_eq!(read_frame(&mut Cursor::new([]), &mut input).unwrap(), None);
        assert_eq!(read_frame(&mut Cursor::new([0]), &mut input).unwrap_err(), "truncated frame header");
    }
    #[test] fn rejects_oversized_length_before_payload_allocation() {
        let mut input = vec![0; MAX_INPUT]; let header = (MAX_INPUT as u32 + 1).to_be_bytes();
        assert_eq!(read_frame(&mut Cursor::new(header), &mut input).unwrap_err(), "frame length exceeds 4608000 bytes");
    }
    #[test] fn rejects_truncated_or_non_rgba_payloads() {
        let mut input = vec![0; MAX_INPUT]; let mut partial = (MAX_INPUT as u32).to_be_bytes().to_vec(); partial.extend([0; 3]);
        assert_eq!(read_frame(&mut Cursor::new(partial), &mut input).unwrap_err(), "truncated frame payload");
        assert_eq!(read_frame(&mut Cursor::new(1_u32.to_be_bytes()), &mut input).unwrap_err(), "invalid RGBA payload length");
    }
    #[test] fn encodes_one_fixed_rgba_frame_with_the_library_bound() {
        let mut encoder = Encoder::new(Subsampling::S420).unwrap(); let cap = encoder.output_cap;
        let jpeg = encoder.encode(&vec![127; MAX_INPUT]).unwrap();
        assert!(jpeg.len() <= cap && jpeg.starts_with(&[0xff, 0xd8]) && jpeg.ends_with(&[0xff, 0xd9]));
    }
}
