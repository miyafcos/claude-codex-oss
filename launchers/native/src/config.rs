use crate::{checked_json as json, platform, proxy};
use regex::Regex;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
};

pub type Env = BTreeMap<String, String>;
// Windows PowerShell 5.1 can treat NAME= as null during launcher execution,
// while passing the empty entry to a native child. Delegate before resolving paths or importing User
// values; deleting all empty entries would change that inherited environment.
const PS_ENV_READS: &[&str] = &[
    "ANTHROPIC_BASE_URL",
    "APPDATA",
    "CCPROXY_CLAUDE_COMPAT_TRACE_OPT",
    "CCPROXY_ERROR_LOG_OPT",
    "CCPROXY_PARALLEL_WORKERS",
    "CLAUDE_CODEX_BACKEND",
    "CLAUDE_CODEX_BANNER",
    "CLAUDE_CODEX_CLAUDE_EXE",
    "CLAUDE_CODEX_FCC_HEALTH_URL",
    "CLAUDE_CODEX_FCC_SERVER_EXE",
    "CLAUDE_CODEX_HOME",
    "CLAUDE_CODEX_KEEP_STDIN",
    "CLAUDE_CODEX_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODEX_PROXY_CMD",
    "CLAUDE_CODEX_PROXY_FASTPATH",
    "CLAUDE_CODEX_PROXY_TOKEN",
    "CLAUDE_CODEX_PROXY_URL",
    "CLAUDE_CODEX_SKILL_PROFILE",
    "CLAUDE_CODEX_SKIP_FCC_START",
    "CLAUDE_CODEX_SKIP_ROUTER_SMOKE",
    "CLAUDE_CODEX_ULTRACODE",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "FCC_ENV_FILE",
    "FUGU_API_KEY",
    "HOST",
    "LOCALAPPDATA",
    "MYCMUX_PANE_SESSION_ID",
    "MYCMUX_RUNTIME_DIR",
    "MYCMUX_TAB_ID",
    "OLLAMA_BASE_URL",
    "USERPROFILE",
    "TEMP",
    "TMP",
];
pub fn empty_ps_environment(env: &Env) -> bool {
    env.iter().any(|(name, value)| {
        value.is_empty() && (PS_ENV_READS.contains(&name.as_str()) || name.starts_with("MYCMUX_"))
    })
}
// Normalized UTF-8 text: remove the BOM and fold CRLF to LF, nothing else.
// Each version records the observable difference, not just its digest.
pub const SUPPORTED_PS_VERSIONS: [(&str, bool); 3] = [
    (
        "1f48ee8b042c2258753cfeed416fd71182f1ee984f811486109d52eacfc8f372",
        false,
    ), // deployed, no alias notice
    (
        "d6bb8b3d471ecbcfc9981c80d5cd0728bf8239da487de5dcb078257e92eb8480",
        true,
    ), // repository, alias notice
    (
        "63c4e4fd0e8ee5bfe215155b82e6587f6827d5781eb7174c75be28314cefe48a",
        false,
    ),
];
pub fn get<'a>(env: &'a Env, name: &str) -> Option<&'a str> {
    env.get(name).filter(|s| !s.is_empty()).map(String::as_str)
}
pub fn child_env_name(name: &str) -> &str {
    if name == "PSEXECUTIONPOLICYPREFERENCE" {
        "PSExecutionPolicyPreference"
    } else {
        name
    }
}
#[derive(Debug, Clone)]
pub struct Paths {
    pub home: PathBuf,
    pub config: PathBuf,
    pub data: PathBuf,
    pub state: PathBuf,
    pub proxy_launcher: PathBuf,
    pub claude: PathBuf,
    pub ps: PathBuf,
    pub proxy_home: PathBuf,
    pub plugin_root: PathBuf,
    pub appdata: PathBuf,
    pub cache: PathBuf,
}
impl Paths {
    pub fn new(exe: &Path, env: &Env) -> io::Result<Self> {
        let home = get(env, "CLAUDE_CODEX_HOME")
            .map(PathBuf::from)
            .map(Ok)
            .unwrap_or_else(|| platform::known_folder(false).map(PathBuf::from))?;
        let user = get(env, "USERPROFILE")
            .map(PathBuf::from)
            .map(Ok)
            .unwrap_or_else(|| platform::known_folder(false).map(PathBuf::from))?;
        let appdata = get(env, "APPDATA")
            .map(PathBuf::from)
            .map(Ok)
            .unwrap_or_else(|| platform::known_folder(true).map(PathBuf::from))?;
        // Join-Path's filesystem provider emits Windows separators for roots
        // supplied with forward slashes. Keep raw executable overrides raw.
        let normalize = |p: PathBuf| -> io::Result<PathBuf> {
            Ok(PathBuf::from(
                p.to_str()
                    .ok_or_else(|| io::Error::other("profile path encoding"))?
                    .replace('/', "\\"),
            ))
        };
        let home = normalize(home)?;
        let user = normalize(user)?;
        let appdata = normalize(appdata)?;
        let config = home.join(r".claude-codex\config");
        let state = home.join(r".claude-codex\state");
        Ok(Self {
            config,
            data: home.join(r".claude-codex\data"),
            cache: state.join("proxy-fastpath-native.json"),
            state,
            proxy_launcher: get(env, "CLAUDE_CODEX_PROXY_CMD")
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join(r"bin\claude-codex-proxy.cmd")),
            claude: get(env, "CLAUDE_CODEX_CLAUDE_EXE")
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join(r".local\bin\claude.exe")),
            ps: exe.with_file_name("claude-codex.ps1"),
            proxy_home: user.join(".claude-codex-proxy"),
            plugin_root: user.join(r".config\ccproxy\plugins"),
            appdata,
            home,
        })
    }
}
#[derive(Debug)]
pub struct Models {
    pub backend: String,
    pub base: String,
    pub default: String,
    pub context: Option<i32>,
    pub aliases: BTreeMap<String, String>,
    pub display: BTreeMap<String, String>,
    pub caps: String,
}
fn string_default(value: Option<&Value>, default: &str) -> Result<String, &'static str> {
    match value {
        None => Ok(default.into()),
        Some(v) if !json::truthy(v) => Ok(default.into()),
        Some(Value::String(s)) => Ok(s.clone()),
        _ => Err("models-ambiguous"),
    }
}
fn string_map(value: Option<&Value>) -> Result<BTreeMap<String, String>, &'static str> {
    let mut out = BTreeMap::new();
    if let Some(v) = value {
        for (k, v) in v.as_object().ok_or("models-ambiguous")? {
            out.insert(k.clone(), v.as_str().ok_or("models-ambiguous")?.into());
        }
    }
    Ok(out)
}
pub fn models(path: &Path, backend: Option<String>) -> Result<Models, &'static str> {
    let content = proxy::text(path).map_err(|_| "models-unreadable")?;
    let v = json::parse(&content).map_err(|e| {
        if e.is_data() {
            "models-ambiguous"
        } else {
            "models-unreadable"
        }
    })?;
    if v["schemaversion"].as_i64() != Some(2) {
        return Err("models-schema");
    }
    let backends = v["backends"].as_object().ok_or("models-schema")?;
    let backend = backend
        .filter(|s| !s.is_empty())
        .unwrap_or(string_default(v.get("defaultbackend"), "gpt")?)
        .to_lowercase();
    if !["gpt", "claude"].contains(&backend.as_str()) {
        return Err("backend-unsupported");
    }
    let b = backends
        .get(&backend)
        .ok_or("backend-missing")?
        .as_object()
        .ok_or("models-ambiguous")?;
    // In StrictMode an empty PSCustomObject has no Properties.Name member.
    // Get-ModelConfig catches that error and restores the complete defaults.
    if b.is_empty() {
        return Err("models-ambiguous");
    }
    let default = string_default(b.get("defaultmodel"), "gpt-5.6-sol")?;
    let base = string_default(b.get("baseurlpath"), "/codex")?;
    let aliases = string_map(b.get("aliases"))?;
    let display = string_map(b.get("display"))?;
    let caps = match b.get("capabilities") {
        None => String::new(),
        Some(v) if !json::truthy(v) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(a)) => a
            .iter()
            .map(|v| v.as_str().ok_or("models-ambiguous"))
            .collect::<Result<Vec<_>, _>>()?
            .join(","),
        _ => return Err("models-ambiguous"),
    };
    let context = match b.get("contexttokens") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => json::positive_int(s),
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => json::positive_int(&n.to_string()),
        _ => return Err("models-ambiguous"),
    };
    Ok(Models {
        backend,
        base,
        default,
        aliases,
        display,
        caps,
        context,
    })
}
pub struct Eligible {
    pub models: Models,
    pub args: Vec<String>,
    pub env: Env,
    pub aliases_notice: bool,
    pub token: String,
    pub base: String,
}
pub fn preflight(paths: &Paths, args: &[String], original: &Env) -> Result<Eligible, &'static str> {
    let ps = proxy::text(&paths.ps).map_err(|_| "ps-launcher-missing")?;
    let digest = proxy::sha(ps.as_bytes());
    let aliases_notice = SUPPORTED_PS_VERSIONS
        .iter()
        .find(|(s, _)| *s == digest)
        .ok_or("ps-launcher-unknown")?
        .1;
    // CLR/PowerShell and the Rust CRT tokenize unquoted line breaks and
    // non-space/tab whitespace differently. Delegate the original command
    // tail instead of silently changing those arguments.
    if args.iter().any(|s| {
        s.chars()
            .any(|c| c.is_whitespace() && c != ' ' && c != '\t')
    }) {
        return Err("argument-whitespace");
    }
    if get(original, "MYCMUX_TAB_ID").is_some_and(|s| s.ends_with('\n')) {
        return Err("tab-id-linebreak");
    }
    if [
        &paths.home,
        &paths.proxy_home,
        &paths.appdata,
        &paths.proxy_launcher,
        &paths.claude,
        &paths.ps,
    ]
    .iter()
    .any(|p| ambiguous_path(p))
        || ["MYCMUX_RUNTIME_DIR", "TEMP", "TMP"]
            .iter()
            .any(|name| get(original, name).is_some_and(|value| ambiguous_path(Path::new(value))))
    {
        return Err("paths-ambiguous");
    }
    if get(original, "CLAUDE_CODEX_SKILL_PROFILE").is_some_and(|s| !s.eq_ignore_ascii_case("full"))
    {
        return Err("skill-profile");
    }
    if get(original, "CLAUDE_CODEX_ULTRACODE") == Some("1") {
        return Err("ultracode");
    }
    if get(original, "CLAUDE_CODEX_BANNER") == Some("1") {
        return Err("banner");
    }
    let mut requested = None;
    let mut filtered = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        let lower = a.to_ascii_lowercase();
        if lower == "--backend" {
            i += 1;
            if i >= args.len() || args[i].starts_with('-') {
                return Err("backend-value-missing");
            }
            requested = Some(args[i].clone());
        } else if lower.starts_with("--backend=") {
            requested = Some(a[10..].into());
        } else {
            filtered.push(a.clone());
        }
        i += 1;
    }
    if requested.as_ref().is_none_or(|s| s.is_empty()) {
        requested = get(original, "CLAUDE_CODEX_BACKEND").map(str::to_owned);
    }
    if requested
        .as_ref()
        .is_some_and(|s| !s.eq_ignore_ascii_case("gpt") && !s.eq_ignore_ascii_case("claude"))
    {
        return Err("backend-unsupported");
    }
    let models = models(&paths.config.join("models.json"), requested)?;
    if !original.contains_key("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
        && original
            .get("CLAUDE_CODEX_MAX_CONTEXT_TOKENS")
            .is_some_and(|s| json::positive_int(s).is_none())
    {
        return Err("context-override-invalid");
    }
    if !paths.proxy_launcher.exists() {
        return Err("proxy-launcher-missing");
    }
    if !paths.claude.exists() {
        return Err("claude-exe-missing");
    }
    // Script overrides need PowerShell's in-process invocation semantics.
    // Native usual launches use the installed .cmd proxy and .exe CLI.
    let proxy_ext = paths
        .proxy_launcher
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if !paths.proxy_launcher.is_file()
        || !["cmd", "bat", "exe", "com"].contains(&proxy_ext.as_str())
    {
        return Err("proxy-launcher-kind");
    }
    let claude_ext = paths
        .claude
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if !paths.claude.is_file() || !["exe", "com"].contains(&claude_ext.as_str()) {
        return Err("claude-exe-kind");
    }
    let mut env = original.clone();
    // This variable is set by the PowerShell host before its launcher body.
    env.insert("PSEXECUTIONPOLICYPREFERENCE".into(), "Bypass".into());
    // The PowerShell host also appends .CPL to PATHEXT when it is missing.
    let pathext = env.get("PATHEXT").cloned().unwrap_or_default();
    let has_ext = |ext: &str| pathext.split(';').any(|x| x.trim().eq_ignore_ascii_case(ext));
    // Without .EXE/.CMD the host cannot run the proxy .cmd the way the exe does.
    if !has_ext(".EXE") || !has_ext(".CMD") {
        return Err("pathext-unusual");
    }
    if !has_ext(".CPL") {
        let sep = if pathext.is_empty() || pathext.ends_with(';') { "" } else { ";" };
        env.insert("PATHEXT".into(), format!("{pathext}{sep}.CPL"));
    }
    import_user(&mut env, "FUGU_API_KEY")?;
    import_user(&mut env, "CLAUDE_CODEX_PROXY_TOKEN")?;
    let token = get(&env, "CLAUDE_CODEX_PROXY_TOKEN")
        .filter(|s| !s.trim().is_empty())
        .ok_or("proxy-token-missing")?
        .to_string();
    for name in ["skills", "rules", "commands", "agents", "plugins"] {
        if !paths.config.join(name).exists() {
            return Err("config-resource-missing");
        }
    }
    let link = paths.config.join("CLAUDE.md");
    let source = paths.home.join(r".claude\CLAUDE.md");
    if !link.exists() || !source.exists() {
        return Err("claude-md-missing");
    }
    if !md_equal(&link, &source) {
        return Err("claude-md-unverified");
    }
    let base = get(&env, "CLAUDE_CODEX_PROXY_URL")
        .unwrap_or("http://127.0.0.1:4026")
        .trim_end_matches('/')
        .to_string();
    Ok(Eligible {
        models,
        args: filtered,
        env,
        aliases_notice,
        token,
        base,
    })
}
fn ambiguous_path(path: &Path) -> bool {
    !path.is_absolute() || path.to_string_lossy().contains(['[', ']', '*', '?', '`'])
}

/// Only accept the raw Windows command-tail grammar whose first split agrees
/// with powershell.exe -File: bare tokens or one nonempty pair of quotes around
/// a whole token. The untouched UTF-16 tail is retained for every delegation.
pub fn validate_command_tail(tail: &str, args: &[String]) -> Result<(), &'static str> {
    if tail
        .chars()
        .any(|c| c.is_whitespace() && c != ' ' && c != '\t')
    {
        return Err("argument-whitespace");
    }
    // powershell.exe -File treats U+2013/2014/2015 as dashes too, and splits
    // "<dash>name:value" even inside quotes (2026-10-16 measured).
    if tail.contains("--%")
        || args.iter().any(|s| s.starts_with('-') && s.contains(':'))
        || args.iter().any(|s| s.starts_with(['\u{2013}', '\u{2014}', '\u{2015}']))
    {
        return Err("argument-host");
    }
    if tail.contains(['$', '`', '@', ';', '&', '|', '^', '%', '!', '\'']) {
        return Err("argument-shell");
    }
    if tail.contains("\"\"") {
        return Err("argument-quoting");
    }
    let chars: Vec<char> = tail.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        if matches!(chars[i], ' ' | '\t') {
            i += 1;
            continue;
        }
        if chars[i] == '"' {
            i += 1;
            while i < chars.len() && chars[i] != '"' {
                i += 1;
            }
            if i == chars.len() || chars[i - 1] == '\\' {
                return Err("argument-quoting");
            }
            i += 1;
            if i < chars.len() && !matches!(chars[i], ' ' | '\t') {
                return Err("argument-quoting");
            }
        } else {
            while i < chars.len() && !matches!(chars[i], ' ' | '\t') {
                if chars[i] == '"' {
                    return Err("argument-quoting");
                }
                i += 1;
            }
        }
    }
    Ok(())
}
fn import_user(env: &mut Env, name: &str) -> Result<(), &'static str> {
    if get(env, name).is_none() {
        if let Some(value) = platform::user_env(name)? {
            if !value.is_empty() {
                env.insert(name.into(), value);
            }
        }
    }
    Ok(())
}
fn md_equal(link: &Path, source: &Path) -> bool {
    use std::os::windows::fs::MetadataExt;
    let result = (|| {
        let a = fs::symlink_metadata(link).ok()?;
        let b = fs::symlink_metadata(source).ok()?;
        if a.file_attributes() & 0x400 == 0
            && b.file_attributes() & 0x400 == 0
            && a.len() != b.len()
        {
            return Some(false);
        }
        Some(proxy::sha(&fs::read(link).ok()?) == proxy::sha(&fs::read(source).ok()?))
    })();
    result == Some(true)
}
pub struct Launch {
    pub env: Env,
    pub args: Vec<String>,
    pub has_print: bool,
    pub mapping: Option<(PathBuf, String)>,
    pub notice: Option<String>,
}
pub fn build(paths: &Paths, e: &Eligible) -> io::Result<Launch> {
    let mut env = e.env.clone();
    let m = &e.models;
    fn set(env: &mut Env, key: &str, value: &str) {
        if value.is_empty() {
            env.remove(key);
        } else {
            env.insert(key.into(), value.into());
        }
    }
    for (k, v) in [
        // powershell.exe -ExecutionPolicy Bypass sets this process variable
        // before evaluating the launcher. Preserve the full child environment.
        ("PSEXECUTIONPOLICYPREFERENCE", "Bypass".into()),
        (
            "CLAUDE_CONFIG_DIR",
            paths.config.to_string_lossy().into_owned(),
        ),
        ("XDG_DATA_HOME", paths.data.to_string_lossy().into_owned()),
        ("XDG_STATE_HOME", paths.state.to_string_lossy().into_owned()),
        ("ANTHROPIC_BASE_URL", e.base.clone() + &m.base),
        ("ANTHROPIC_AUTH_TOKEN", e.token.clone()),
        ("ANTHROPIC_API_KEY", e.token.clone()),
        ("ENABLE_TOOL_SEARCH", "true".into()),
        ("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", "1".into()),
        ("CLAUDE_CODE_ENABLE_TODO_TOOLS", "1".into()),
        (
            "CLAUDE_CODE_SUBAGENT_MODEL",
            m.aliases.get("subagent").unwrap_or(&m.default).clone(),
        ),
        ("ANTHROPIC_DEFAULT_MODEL", m.default.clone()),
        ("ANTHROPIC_MODEL", m.default.clone()),
    ] {
        set(&mut env, k, &v);
    }
    if !env.contains_key("CLAUDE_CODE_MAX_CONTEXT_TOKENS") {
        if let Some(n) = env
            .get("CLAUDE_CODEX_MAX_CONTEXT_TOKENS")
            .and_then(|s| json::positive_int(s))
            .or(m.context)
        {
            set(&mut env, "CLAUDE_CODE_MAX_CONTEXT_TOKENS", &n.to_string());
        }
    }
    if m.backend == "gpt" {
        set(&mut env, "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000");
        set(&mut env, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "60");
    }
    let display = m
        .display
        .get("name")
        .map(String::as_str)
        .unwrap_or("GPT-5.6");
    for family in ["FABLE", "OPUS", "SONNET", "HAIKU"] {
        let key = family.to_ascii_lowercase();
        let name = m
            .display
            .get(&(key.clone() + "name"))
            .map(String::as_str)
            .unwrap_or(display);
        let desc = m
            .display
            .get(&(key.clone() + "description"))
            .map(String::as_str)
            .unwrap_or("OpenAI subscription route via Codex backend");
        for (suffix, value) in [
            (
                "",
                m.aliases
                    .get(&key)
                    .map(String::as_str)
                    .unwrap_or(&m.default),
            ),
            ("_NAME", name),
            ("_DESCRIPTION", desc),
            ("_SUPPORTED_CAPABILITIES", &m.caps),
        ] {
            set(
                &mut env,
                &format!("ANTHROPIC_DEFAULT_{family}_MODEL{suffix}"),
                value,
            );
        }
    }
    for k in [
        "ANTHROPIC_CUSTOM_MODEL_OPTION",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
    ] {
        set(&mut env, k, "");
    }
    let has = |flag: &str, equals: bool| {
        e.args.iter().any(|s| {
            s.eq_ignore_ascii_case(flag)
                || (equals
                    && s.to_ascii_lowercase()
                        .starts_with(&(flag.to_ascii_lowercase() + "=")))
        })
    };
    let has_print = has("-p", false) || has("--print", false);
    let mut args = Vec::new();
    let icon = paths.config.join("blue-icon.settings.json");
    if m.backend == "gpt" && !has("--settings", true) && icon.exists() {
        args.extend(["--settings".into(), icon.to_string_lossy().into_owned()]);
    }
    if m.backend == "gpt"
        && !has("--effort", true)
        && get(&env, "CLAUDE_CODE_EFFORT_LEVEL").is_none()
    {
        args.extend(["--effort".into(), "xhigh".into()]);
    }
    if has_print {
        set(&mut env, "MCP_CONNECTION_NONBLOCKING", "true");
        if !has("--mcp-config", true) && !has("--strict-mcp-config", false) {
            args.extend([
                "--strict-mcp-config".into(),
                "--mcp-config".into(),
                paths
                    .config
                    .join("empty-mcp.json")
                    .to_string_lossy()
                    .into_owned(),
            ]);
        }
    } else {
        set(&mut env, "MCP_CONNECTION_NONBLOCKING", "");
    }
    let notice = if e.aliases_notice && !has_print && m.backend == "gpt" {
        Some(format!("[claude-codex] GPT model aliases: opus={}, sonnet={}, haiku={}, subagent={}; use explicit claude-* IDs for Claude.",m.aliases.get("opus").map(String::as_str).unwrap_or(""),m.aliases.get("sonnet").map(String::as_str).unwrap_or(""),m.aliases.get("haiku").map(String::as_str).unwrap_or(""),get(&env,"CLAUDE_CODE_SUBAGENT_MODEL").unwrap_or("")))
    } else {
        None
    };
    set(&mut env, "CLAUDECODE", "");
    set(&mut env, "CLAUDE_CODE_ENTRYPOINT", "");
    let session_override=Regex::new(r"(?i)^(--session-id|--resume|--continue|--fork-session|--print|--no-session-persistence|--help|--version)(=|$)|^-[prc]|^-[hv]$|^(agents|attach|auth|auto-mode|doctor|gateway|import|install|logs|mcp|plugin|plugins|purge|respawn|rm|setup-token|stop|kill|ultrareview|update|upgrade)$").unwrap();
    let uuid = Regex::new(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$").unwrap();
    let mut mapping = None;
    if !has_print
        && get(&env, "MYCMUX_PANE_SESSION_ID").is_some()
        && get(&env, "MYCMUX_TAB_ID").is_some_and(|s| uuid.is_match(s))
        && !e.args.iter().any(|s| session_override.is_match(s))
    {
        let id = platform::guid()?;
        let root = get(&env, "MYCMUX_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| paths.home.join(".mycmux"));
        mapping = Some((
            root.join("pane-sessions")
                .join(format!("{}.txt", env["MYCMUX_TAB_ID"])),
            format!("claude-codex:{id}\n"),
        ));
        args.extend(["--session-id".into(), id]);
    }
    args.extend(e.args.clone());
    Ok(Launch {
        env,
        args,
        has_print,
        mapping,
        notice,
    })
}
pub fn env_diff(before: &Env, after: &Env) -> Value {
    let mut diff = serde_json::Map::new();
    for key in before.keys().chain(after.keys()) {
        if before.get(key) == after.get(key) {
            continue;
        }
        let hidden = key.contains("TOKEN")
            || key.contains("API_KEY")
            || key.contains("SECRET")
            || key.contains("PASSWORD");
        diff.insert(
            key.clone(),
            match after.get(key) {
                Some(_) if hidden => json!("<redacted>"),
                Some(s) => json!(s),
                None => Value::Null,
            },
        );
    }
    Value::Object(diff)
}

/// Match Windows PowerShell 5.1's legacy native argument binder, including
/// dropping empty arguments and its treatment of embedded quotes/trailing
/// backslashes. Command::args uses CRT escaping, which would change argv.
pub fn legacy_arguments(args: &[String]) -> String {
    args.iter()
        .filter(|s| !s.is_empty())
        .map(|s| {
            let mut quotes = 0;
            let needs_quotes = s.chars().any(|c| {
                if c == '"' {
                    quotes += 1;
                }
                c.is_whitespace() && quotes % 2 == 0
            });
            if needs_quotes {
                format!("\"{s}\"")
            } else {
                s.clone()
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn redaction() {
        let before = Env::new();
        let after = Env::from([("ANTHROPIC_API_KEY".into(), "secret".into())]);
        assert_eq!(env_diff(&before, &after)["ANTHROPIC_API_KEY"], "<redacted>");
    }
    #[test]
    fn legacy_quotes() {
        assert_eq!(
            legacy_arguments(&["a b".into(), "".into(), "x\"y z\"q".into(), "end \\".into()]),
            "\"a b\" x\"y z\"q \"end \\\""
        );
    }
    #[test]
    fn empty_process_values_delegate_without_normalizing() {
        let mut env = Env::new();
        env.insert("CLAUDE_CODE_MAX_CONTEXT_TOKENS".into(), String::new());
        assert!(empty_ps_environment(&env));
        assert_eq!(env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS").unwrap(), "");
        env.clear();
        env.insert("MYCMUX_FUTURE".into(), String::new());
        assert!(empty_ps_environment(&env));
        env.clear();
        env.insert("UNRELATED".into(), String::new());
        assert!(!empty_ps_environment(&env));
    }
    #[test]
    fn only_proven_command_tail_grammar_is_native() {
        for tail in [
            "",
            "-p \"hello world\"",
            "--resume abc",
            "--continue",
            "-p plain\\",
            "-p\t\"a\tb\"",
        ] {
            assert!(validate_command_tail(tail, &[]).is_ok(), "{tail}");
        }
        for tail in [
            "-p \"a \"\"b c\"\" d\"",
            "-p x\"y z\"q",
            "-p \"a b\\\"",
            "-p \"\"",
            "--% -p hi",
            "-p x--%y",
        ] {
            assert!(validate_command_tail(tail, &[]).is_err(), "{tail}");
        }
        assert_eq!(
            validate_command_tail("-p:x", &["-p:x".into()]),
            Err("argument-host")
        );
        assert!(ambiguous_path(Path::new(r"C:\fake[1].exe")));
        assert!(!ambiguous_path(Path::new(r"C:\normal path\claude.exe")));
    }
}
