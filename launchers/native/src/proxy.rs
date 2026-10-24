use crate::{
    checked_json as json,
    config::Paths,
    platform::{self, Acquired, Mutex},
};
use regex::Regex;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::{self, Read, Write},
    net::{TcpStream, ToSocketAddrs},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant, UNIX_EPOCH},
};

pub const CONTRACT_SHA: &str = "c750ff8c849b7b5da05689289e0d0d11b107364c7d1418129ea6081e9035b587";
pub const WAIT_NOTICE: &str =
    "[claude-codex] Waiting for another claude-codex launch to finish starting the proxy...";
pub fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub fn text(path: &Path) -> io::Result<String> {
    fs::read_to_string(path).map(|s| s.trim_start_matches('\u{feff}').replace("\r\n", "\n"))
}

#[derive(Clone, Debug)]
pub struct Probe {
    pub stamp: String,
    pub url: String,
    pub ready_id: String,
    pub lock: String,
    pub paths: Vec<PathBuf>,
    pub contract: String,
}
impl Probe {
    pub fn json(&self) -> Value {
        json!({"stamp":self.stamp,"proxy_url":self.url,"ready_model_id":self.ready_id,"lock_name":self.lock,"paths":self.paths,"contract_sha256":self.contract})
    }
}

fn collect_files(root: &Path, out: &mut Vec<PathBuf>, depth: usize) -> io::Result<()> {
    if depth > 128 {
        return Err(io::Error::other("recursive plugin path"));
    }
    for entry in fs::read_dir(root)? {
        let path = entry?.path();
        let meta = fs::metadata(&path)?;
        if meta.is_dir() {
            if !path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .eq_ignore_ascii_case("__pycache__")
            {
                collect_files(&path, out, depth + 1)?;
            }
        } else {
            let ext = path
                .extension()
                .unwrap_or_default()
                .to_string_lossy()
                .to_ascii_lowercase();
            if ext != "pyc" && ext != "pyo" {
                out.push(path);
            }
        }
    }
    Ok(())
}
pub fn probe(paths: &Paths, env: &BTreeMap<String, String>) -> Option<Probe> {
    probe_inner(paths, env).ok()
}
fn probe_inner(paths: &Paths, env: &BTreeMap<String, String>) -> io::Result<Probe> {
    let invalid = || io::Error::other("unsupported proxy contract");
    let script = paths.proxy_home.join(r"scripts\claude-codex-proxy.ps1");
    let body = text(&script)?;
    let host_re = Regex::new(r#"(?m)^\$HostName = "([0-9A-Za-z.:-]+)"$"#).unwrap();
    let port_re = Regex::new(r"(?m)^\$Port = ([0-9]+)$").unwrap();
    let lock_re = Regex::new(r#"Threading\.Mutex\]::new\(\$false, "([^"$`]+)"\)"#).unwrap();
    let ready_re = Regex::new(r#"\$ids -contains "([^"$`]+)""#).unwrap();
    let host = host_re.captures(&body).ok_or_else(invalid)?;
    let port = port_re.captures(&body).ok_or_else(invalid)?;
    let lock = lock_re.captures(&body).ok_or_else(invalid)?;
    let ready = ready_re.captures(&body).ok_or_else(invalid)?;
    let contract_body = body
        .replace(&host[0], "$HostName = \"<host>\"")
        .replace(&port[0], "$Port = <port>")
        .replace(&format!("\"{}\"", &lock[1]), "\"<lock>\"");
    let contract = sha(contract_body.as_bytes());
    if contract != CONTRACT_SHA {
        return Err(invalid());
    }
    let start = body
        .find("function Get-RuntimeGeneration")
        .ok_or_else(invalid)?;
    let end = body[start + 1..]
        .find("\nfunction ")
        .map(|n| start + 1 + n)
        .unwrap_or(body.len());
    let relatives = Regex::new(r#""([A-Za-z0-9_\\]+\.py)""#).unwrap();
    let relative_paths: Vec<_> = relatives.captures_iter(&body[start..end]).collect();
    if relative_paths.is_empty() {
        return Err(invalid());
    }
    let site = paths
        .appdata
        .join(r"uv\tools\ccproxy-api\Lib\site-packages");
    let config = paths.proxy_home.join("config.toml");
    let mut inputs = vec![
        config.clone(),
        paths.proxy_home.join("install-manifest.json"),
        script,
        paths.proxy_home.join(r"scripts\ccproxy-lb.py"),
        paths.proxy_launcher.clone(),
    ];
    for c in relative_paths {
        inputs.push(site.join("ccproxy").join(&c[1]));
    }
    if paths.plugin_root.exists() {
        collect_files(&std::path::absolute(&paths.plugin_root)?, &mut inputs, 0)?;
    }
    if site.exists() {
        for dir in fs::read_dir(&site)? {
            let dir = dir?;
            let name = dir.file_name().to_string_lossy().to_ascii_lowercase();
            if dir.path().is_dir()
                && name.starts_with("ccproxy_api-")
                && name.ends_with(".dist-info")
            {
                inputs.push(std::path::absolute(dir.path())?.join("RECORD"));
            }
        }
    }
    // Ordering is deliberately ordinal, independent of Sort-Object's culture.
    let mut unique = BTreeMap::new();
    for path in inputs {
        unique
            .entry(path.to_string_lossy().to_lowercase())
            .or_insert(path);
    }
    let inputs: Vec<_> = unique.into_values().collect();
    let url = format!("http://{}:{}", &host[1], &port[1]);
    let opt = |name: &str| {
        env.get(name)
            .filter(|v| !v.is_empty())
            .map(String::as_str)
            .unwrap_or("0")
    };
    let mut parts = vec![
        "stamp=native-1".into(),
        format!("contract={contract}"),
        format!("url={url}"),
        format!("lock={}", &lock[1]),
        format!("trace={}", opt("CCPROXY_CLAUDE_COMPAT_TRACE_OPT")),
        format!("error_log={}", opt("CCPROXY_ERROR_LOG_OPT")),
    ];
    if config.exists() {
        parts.push(format!("config_sha={}", sha(&fs::read(&config)?)));
    }
    for path in &inputs {
        let part = match fs::metadata(path) {
            Ok(m) if m.is_file() => format!(
                "{}|{}|{}",
                path.display(),
                m.len(),
                m.modified()?
                    .duration_since(UNIX_EPOCH)
                    .map_err(io::Error::other)?
                    .as_nanos()
            ),
            _ => format!("{}|missing", path.display()),
        };
        parts.push(part);
    }
    Ok(Probe {
        stamp: sha(parts.join("\n").as_bytes()),
        url,
        lock: lock[1].into(),
        ready_id: ready[1].into(),
        paths: inputs,
        contract,
    })
}
pub fn enabled(env: &BTreeMap<String, String>) -> bool {
    !["0", "false", "off", "no"].contains(
        &env.get("CLAUDE_CODEX_PROXY_FASTPATH")
            .map(|s| s.trim().to_ascii_lowercase())
            .unwrap_or_default()
            .as_str(),
    ) && env
        .get("CCPROXY_PARALLEL_WORKERS")
        .is_none_or(|s| s.trim().is_empty())
}
pub fn pid(paths: &Paths) -> Option<i32> {
    text(&paths.proxy_home.join(r"run\ccproxy.pid"))
        .ok()?
        .trim()
        .parse()
        .ok()
}
fn managed(paths: &Paths) -> Option<(i32, String)> {
    let pid = pid(paths)?;
    if !platform::process_alive(pid) {
        return None;
    }
    let manifest =
        json::parse(&text(&paths.proxy_home.join(r"run\ccproxy-manifest.json")).ok()?).ok()?;
    for key in ["mode", "worker_count", "runtime_generation"] {
        manifest.get(key)?;
    }
    let generation = manifest["runtime_generation"].as_str()?.to_string();
    if generation.trim().is_empty() {
        return None;
    }
    Some((pid, generation))
}

/// A direct local HTTP request, with an absolute two-second deadline and no
/// system/environment proxy discovery. Both Content-Length and chunked bodies
/// are handled without waiting for a keep-alive socket to close.
pub fn models_ready(probe: &Probe, token: &str) -> bool {
    models_ready_inner(probe, token).unwrap_or(false)
}
fn models_ready_inner(probe: &Probe, token: &str) -> io::Result<bool> {
    if token.contains(['\r', '\n']) {
        return Ok(false);
    }
    let authority = probe
        .url
        .strip_prefix("http://")
        .ok_or_else(|| io::Error::other("HTTP only"))?;
    // The contracted PS text builds an unbracketed URL. HttpWebRequest rejects
    // that URL for an IPv6 literal; do not reinterpret it as a socket address.
    if authority.matches(':').count() != 1 {
        return Ok(false);
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    let left = || {
        deadline
            .checked_duration_since(Instant::now())
            .filter(|d| !d.is_zero())
            .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "models probe"))
    };
    let mut connection = None;
    for addr in authority.to_socket_addrs()? {
        if let Ok(stream) = TcpStream::connect_timeout(&addr, left()?) {
            connection = Some(stream);
            break;
        }
    }
    let mut stream = connection.ok_or_else(|| io::Error::other("connection failed"))?;
    stream.set_write_timeout(Some(left()?))?;
    write!(stream,"GET /router/v1/models HTTP/1.1\r\nHost: {authority}\r\nAuthorization: Bearer {token}\r\nanthropic-version: 2023-06-01\r\nConnection: close\r\n\r\n")?;
    let mut buf = Vec::new();
    let mut head_end = None;
    let mut length = None;
    let mut chunked = false;
    loop {
        stream.set_read_timeout(Some(left()?))?;
        let mut piece = [0u8; 8192];
        let n = stream.read(&mut piece)?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&piece[..n]);
        if buf.len() > 16 * 1024 * 1024 {
            return Ok(false);
        }
        if head_end.is_none() {
            if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                let header = String::from_utf8_lossy(&buf[..i]);
                let status = header
                    .lines()
                    .next()
                    .unwrap_or("")
                    .split_whitespace()
                    .nth(1)
                    .unwrap_or("");
                if status != "200" {
                    return Ok(false);
                }
                for line in header.lines().skip(1) {
                    if let Some((key, val)) = line.split_once(':') {
                        if key.eq_ignore_ascii_case("content-length") {
                            length = val.trim().parse::<usize>().ok();
                        }
                        if key.eq_ignore_ascii_case("transfer-encoding") {
                            chunked = val
                                .split(',')
                                .any(|s| s.trim().eq_ignore_ascii_case("chunked"));
                        }
                    }
                }
                head_end = Some(i + 4);
            }
        }
        if let Some(i) = head_end {
            if chunked {
                if decode_chunks(&buf[i..]).is_some() {
                    break;
                }
            } else if length.is_some_and(|n| buf.len() - i >= n) {
                break;
            }
        }
    }
    let Some(i) = head_end else { return Ok(false) };
    let body = if chunked {
        decode_chunks(&buf[i..]).ok_or_else(|| io::Error::other("incomplete chunks"))?
    } else {
        let n = length.unwrap_or(buf.len() - i);
        if buf.len() - i < n {
            return Ok(false);
        }
        buf[i..i + n].to_vec()
    };
    let data = json::parse(std::str::from_utf8(&body).unwrap_or("")).ok();
    let Some(data) = data else { return Ok(false) };
    Ok(ready_document(&data, &probe.ready_id))
}
fn ready_document(data: &Value, ready_id: &str) -> bool {
    let Some(items) = data
        .as_object()
        .and_then(|v| v.get("data"))
        .and_then(Value::as_array)
    else {
        return false;
    };
    // StrictMode enumerates the whole list before -contains. A missing id or
    // a non-object anywhere invalidates readiness, even after the marker.
    if !items
        .iter()
        .all(|v| v.as_object().is_some_and(|v| v.contains_key("id")))
    {
        return false;
    }
    items.iter().any(|v| {
        let id = &v["id"];
        // [string] on a one-string array yields that string in PowerShell.
        // Null/numeric/object ids do not equal the contracted model path, but
        // they do not invalidate a separate valid marker in the same list.
        id.as_str()
            .or_else(|| {
                id.as_array()
                    .filter(|a| a.len() == 1)
                    .and_then(|a| a[0].as_str())
            })
            .is_some_and(|s| s.eq_ignore_ascii_case(ready_id))
    })
}
fn decode_chunks(buf: &[u8]) -> Option<Vec<u8>> {
    let mut pos = 0;
    let mut out = Vec::new();
    loop {
        let end = buf.get(pos..)?.windows(2).position(|w| w == b"\r\n")? + pos;
        let line = std::str::from_utf8(&buf[pos..end])
            .ok()?
            .split(';')
            .next()?;
        let n = usize::from_str_radix(line.trim(), 16).ok()?;
        pos = end + 2;
        if n == 0 {
            let trailers = buf.get(pos..)?;
            if trailers.starts_with(b"\r\n") || trailers.windows(4).any(|w| w == b"\r\n\r\n") {
                return Some(out);
            }
            return None;
        }
        let end = pos.checked_add(n)?;
        out.extend_from_slice(buf.get(pos..end)?);
        if buf.get(end..end + 2)? != b"\r\n" {
            return None;
        }
        pos = end + 2;
    }
}

pub fn fast_path(probe: Option<&Probe>, paths: &Paths, proxy_base: &str, token: &str) -> String {
    let Some(probe) = probe else {
        return "no-probe".into();
    };
    if !proxy_base.eq_ignore_ascii_case(&probe.url) {
        return "other-url".into();
    }
    if !paths.cache.exists() {
        return "no-cache".into();
    }
    let cached = (|| {
        let v = json::parse(&text(&paths.cache).ok()?).ok()?;
        if !v["stamp"].as_str()?.eq_ignore_ascii_case(&probe.stamp) {
            return None;
        }
        let pid = if let Some(n) = v["pid"].as_i64() {
            i32::try_from(n).ok()?
        } else {
            v["pid"].as_str()?.parse().ok()?
        };
        Some((pid, v["runtime_generation"].as_str()?.to_string()))
    })();
    let Some((cached_pid, generation)) = cached else {
        return "stale-cache".into();
    };
    // Even an abandoned maintenance mutex is a miss; Drop releases ownership
    // acquired by WAIT_ABANDONED, just as the PowerShell catch/finally does.
    let lock = (|| {
        let mut lock = Mutex::new(&probe.lock).ok()?;
        if lock.acquire(0).ok()? == Acquired::Yes {
            Some(lock)
        } else {
            None
        }
    })();
    if lock.is_none() {
        return "maintenance".into();
    }
    drop(lock);
    match managed(paths) {
        Some((pid, g)) if pid == cached_pid && g.eq_ignore_ascii_case(&generation) => {}
        _ => return "stale-proxy".into(),
    }
    if token.trim().is_empty() {
        return "no-token".into();
    }
    if !models_ready(probe, token) {
        return "not-ready".into();
    }
    if pid(paths) != Some(cached_pid) {
        return "stale-proxy".into();
    }
    "ok".into()
}
fn save(
    probe: Option<&Probe>,
    paths: &Paths,
    env: &BTreeMap<String, String>,
    output: &str,
) -> io::Result<()> {
    let Some(probe) = probe else { return Ok(()) };
    let reported = Regex::new(r"^(?:already running|started)\b.*?\bpid=([0-9]+)\b").unwrap();
    let pid = output
        .lines()
        .filter_map(|line| {
            reported
                .captures(line)
                .and_then(|c| c[1].parse::<i32>().ok())
        })
        .last();
    let Some(reported_pid) = pid else {
        return Ok(());
    };
    let Some((pid, generation)) = managed(paths) else {
        return Ok(());
    };
    if pid != reported_pid {
        return Ok(());
    }
    if self::probe(paths, env).is_none_or(|after| !after.stamp.eq_ignore_ascii_case(&probe.stamp)) {
        return Ok(());
    }
    fs::write(&paths.cache,json!({"stamp":probe.stamp,"pid":pid,"runtime_generation":generation,"saved_at":platform::timestamp()}).to_string())
}
pub fn ensure(
    probe: Option<&Probe>,
    initial: &str,
    paths: &Paths,
    env: &BTreeMap<String, String>,
    base: &str,
    token: &str,
) -> io::Result<i32> {
    if initial == "ok" {
        return Ok(0);
    }
    let mut launcher = None;
    if let Some(probe) = probe
        .filter(|_| ["no-cache", "stale-cache", "maintenance", "stale-proxy"].contains(&initial))
    {
        if let Ok(mut lock) = Mutex::new(&(probe.lock.clone() + "-launcher")) {
            let started = Instant::now();
            let mut noticed = false;
            while started.elapsed() < Duration::from_secs(180) {
                match lock.acquire(250) {
                    Ok(Acquired::Yes | Acquired::Abandoned) => {
                        launcher = Some(lock);
                        break;
                    }
                    Ok(Acquired::Timeout) => {}
                    Err(_) => break,
                }
                if !noticed && started.elapsed() >= Duration::from_secs(2) {
                    eprintln!("{WAIT_NOTICE}");
                    noticed = true;
                }
            }
        }
    }
    if launcher.is_some() && fast_path(probe, paths, base, token) == "ok" {
        return Ok(0);
    }
    let error = std::env::temp_dir().join(format!(
        "ccproxy-start-{}.err",
        platform::guid()?.replace('-', "")
    ));
    let capture = fs::File::create(&error)?;
    let mut cmd = Command::new(&paths.proxy_launcher);
    cmd.arg("start")
        .stderr(Stdio::from(capture))
        .stdout(Stdio::piped());
    for (k, v) in env {
        if std::env::var(k).ok().as_ref() != Some(v) {
            cmd.env(crate::config::child_env_name(k), v);
        }
    }
    let output = cmd.output()?;
    let code = output.status.code().unwrap_or(1);
    if code != 0 {
        eprintln!(
            "[claude-codex] Proxy startup failed. See {} for details.",
            error.display()
        );
        eprintln!("[claude-codex] Run for full diagnosis: powershell -ExecutionPolicy Bypass -File scripts\\healthcheck.ps1 -Quick");
        return Ok(code);
    }
    if fs::metadata(&error).is_ok_and(|m| m.len() == 0) {
        let _ = fs::remove_file(&error);
    }
    let _ = save(probe, paths, env, &String::from_utf8_lossy(&output.stdout));
    drop(launcher);
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn chunked() {
        assert_eq!(
            decode_chunks(b"3\r\nabc\r\n2;foo=1\r\nde\r\n0\r\n\r\n"),
            Some(b"abcde".to_vec())
        );
        assert_eq!(decode_chunks(b"3\r\na"), None);
        assert_eq!(decode_chunks(b"0\r\n"), None);
    }
    #[test]
    fn contract() {
        assert_eq!(CONTRACT_SHA.len(), 64);
    }
    #[test]
    fn malformed_ready_document_never_short_circuits() {
        let marker = json!({"id":"READY"});
        assert!(ready_document(&json!({"data":[marker.clone()]}), "ready"));
        for id in [Value::Null, json!(1), json!({}), json!(["ready"])] {
            assert!(ready_document(
                &json!({"data":[{"id":id},marker.clone()]}),
                "ready"
            ));
        }
        assert!(ready_document(&json!({"data":[{"id":["ready"]}]}), "ready"));
        for bad in [
            Value::Null,
            json!({}),
            json!(7),
            json!(false),
            json!("x"),
            json!([marker.clone()]),
        ] {
            assert!(!ready_document(
                &json!({"data":[bad.clone(),marker.clone()]}),
                "ready"
            ));
            assert!(!ready_document(
                &json!({"data":[marker.clone(),bad]}),
                "ready"
            ));
        }
        for bad in [
            Value::Null,
            json!({}),
            json!({"data":marker}),
            json!({"data":[]}),
        ] {
            assert!(!ready_document(&bad, "ready"));
        }
    }
}
