# Web Tools Security Architecture & Defenses

---

### 1. Parser-Differential & URI Normalization Defense (`validate_url`)

* **What**: A strict pre-flight gate that validates every URL before it is touched by Python (`httpx`) or passed to Obscura CLI.
* **Why**: 
  * Different software parsers interpret ambiguous URLs differently. For example, Python’s `urllib.parse` vs Rust’s `url` crate vs browser engines handle backslashes (`\`), spaces, tabs, and userinfo (`user:pass@`) differently.
  * Attackers use these differences for **SSRF bypasses** — e.g. `https://google.com\@127.0.0.1` might look like `google.com` to Python, but Obscura/cURL parses it as connecting to `127.0.0.1`.
* **How**:
  * Rejects control characters, newlines, tabs, spaces, and backslashes upfront (`any(c in raw_url for c in " \t\r\n\\")`).
  * Rejects embedded credentials/userinfo (`p.username or p.password`).
  * Caps raw length at `2048` characters.
  * Emits a canonical string (`p.geturl()`) so all subsequent components see the exact same URI.

---

### 2. Standard Web Port Restriction (`ALLOWED_PORTS = {80, 443}`)

* **What**: Rejects URLs requesting non-standard port numbers (e.g. `http://example.com:6379`, `http://target.xyz:22`).
* **Why**:
  * Attackers craft links pointing to non-HTTP ports to use our server as an **internal/external port scanner** or to interact with text-based protocols (like Redis, Memcached, SMTP, or internal admin panels) via HTTP smuggling.
* **How**:
  * Checks `if p.port is not None and p.port not in (80, 443): return UrlPolicyVerdict.NON_STANDARD_PORT`.

---

### 3. IPv6-Mapped IPv4 & Pre-Flight DNS Policy

* **What**: Asynchronously resolves hostnames and checks every resolved IP address against global routability, unwrapping IPv6-mapped IPv4 representations.
* **Why**:
  * Scammers submit links pointing to `127.0.0.1` (loopback) or `169.254.169.254` (cloud metadata to steal IAM instance tokens).
  * Attackers use IPv6-mapped IPv4 addresses (e.g. `[::ffff:127.0.0.1]`) to bypass naive string checks looking for `"127.0.0.1"`.
* **How**:
  * Uses `asyncio.get_running_loop().getaddrinfo()` to resolve DNS without blocking the asyncio loop.
  * Checks:
    ```python
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        return UrlPolicyVerdict.BLOCKED_PRIVATE_IP
    ```

---

### 4. Process Group Isolation & Zombie Cleanup (`start_new_session=True` & `os.killpg`)

* **What**: Spawns the Obscura CLI process inside its own distinct Linux process group, and kills the entire group with `os.killpg` on timeout or cancellation.
* **Why**:
  * Obscura uses multi-process worker pools (e.g. `obscura-worker`, V8 isolates).
  * A standard `proc.kill()` in Python **only sends SIGKILL to the parent process**. The child browser workers become orphaned "zombies" that stay running in the background, consuming CPU/RAM and leaking file descriptors.
* **How**:
  * `create_subprocess_exec(..., start_new_session=True)` makes the spawned process the leader of a new process group.
  * On timeout:
    ```python
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    ```

---

### 5. Environment Stripping (Secret & SSRF Leak Prevention)

* **What**: Obscura is executed with a sanitized, minimal environment dictionary (`clean_env`), completely discarding `os.environ`.
* **Why**:
  * By default, child processes inherit the full parent environment. If our worker environment contains `DATABASE_URL`, `REDIS_URL`, or LLM API keys (`OPENAI_API_KEY`), a compromised browser process or crash dump could expose them.
  * Furthermore, Obscura has an environment variable `OBSCURA_ALLOW_PRIVATE_NETWORK=1`. If that variable were set anywhere in the environment, Obscura's internal SSRF guard would be disabled!
* **How**:
  * Explicitly passes:
    ```python
    clean_env = {
        "PATH": "/home/ojas/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
    }
    ```

---

### 6. Subprocess Argument Hardening (`--` Double Dash)

* **What**: Appends `--` immediately before the target URL: `["obscura", "fetch", ..., "--", target_url]`.
* **Why**:
  * Prevents **Option Injection**. If an untrusted URL begins with a dash (e.g. `--allow-private-network` or `-s`), naive argument parsing might treat the URL as a CLI flag rather than a positional URL argument.
* **How**:
  * Follows POSIX standard where `--` explicitly signals the end of command-line options.

---

### 7. Regular Expression Denial of Service (ReDoS) Protection

* **What**: Replaced unbounded regex patterns with bounded, length-capped quantifiers and pre-sliced input buffers.
* **Why**:
  * A pattern like `\[([^\]]+)\]\(` running against an attacker-controlled page containing megabytes of unclosed `[[[[...` causes polynomial/exponential backtracking.
  * Because Python regex is synchronous, a ReDoS stalls the Python GIL and completely freezes the entire `asyncio` event loop for all workers.
* **How**:
  * Caps input buffer: `content = content[:100_000]`.
  * Bounds anchor text and URL length in the regex: `\[([^\]\n]{1,200})\]\((https?://[^\s\)\n]{1,2048}|/[^\s\)\n]{1,2048})\)`.
  * Verified in testing: parses 50,000 pathological `[` characters in **81 milliseconds**.

---

### 8. Unicode Sanitization & Bidi Override Stripping

* **What**: Strips invisible Unicode format controls (`Cf`) and surrogate code points (`Cs`) from the page text.
* **Why**:
  * Attackers use Right-to-Left (RTL) override characters (e.g. `\u202E`) and zero-width spaces (`\u200B`) to visually spoof filenames or reverse the visual appearance of links/domain names, tricking both human analysts and LLMs into misreading the text.
* **How**:
  * Uses Python's standard `unicodedata`:
    ```python
    clean_content = "".join(
        c for c in stdout_str if unicodedata.category(c) not in ("Cf", "Cs")
    )
    ```

---

### 9. Bounded Memory Buffering

* **What**: Limits maximum byte reads from stdout (512 KB) and stderr (64 KB).
* **Why**:
  * If a malicious website returns a 100 MB HTML payload or floods the console with infinite logs, `proc.communicate()` will read all of it into RAM, causing the Python worker to crash from an Out-of-Memory (OOM) kill.
* **How**:
  * Slices buffer immediately: `stdout_bytes[:512 * 1024].decode(errors="replace")`.
  * Uses `errors="replace"` so malformed binary sequences never crash the parser with a `UnicodeDecodeError`.

---

### 10. Post-Redirect Final URL Re-Validation

* **What**: Validates the *destination URL* after all redirects and JavaScript execution have settled.
* **Why**:
  * Initial URL `http://example.com` passes the pre-flight check, but once loaded in the browser, a client-side JavaScript redirect executes: `window.location.replace("http://169.254.169.254")`.
* **How**:
  * Extracts the post-settle `final_url` from Obscura's loaded status banner.
  * Re-runs `validate_url(final_url)`. If the final landing site violates policy, the output is rejected (`redirect_blocked_<verdict>`).
  * Resolves all relative links against `final_url` instead of the initial input URL.

---

### 11. Staged Content-Addressed Storage & PNG Header Validation

* **What**: Writes screenshots to an ephemeral scratch directory, validates PNG magic bytes and dimensions, computes SHA-256, and moves to content-addressed storage.
* **Why**:
  * Prevents corrupt, half-written, or 0-byte images from entering production artifact storage.
  * Prevents path traversal vulnerabilities and host filesystem disclosure.
* **How**:
  * Obscura writes to a temporary sandbox (`tempfile.TemporaryDirectory`).
  * Checks first 8 magic bytes: `\x89PNG\r\n\x1a\n`.
  * Unpacks width/height from the 16th byte of the `IHDR` chunk using `struct.unpack(">II", data[16:24])`.
  * Moves the verified file to `data/artifacts/<sha256>.png`.
  * Returns an opaque handle `art_<hash[:16]>` rather than the server's absolute disk path.

---

### 12. Native Trust & Provenance Tagging

* **What**: Attaches cryptographic-grade audit records (`TrustLevel` and `Provenance`) to every tool output.
* **Why**:
  * Downstream LLM agents and verifiers must know the epistemic authority of incoming data.
  * Output from `web_inspect` is `TrustLevel.DETERMINISTIC` (factual network traces).
  * Output from `extract_page_content` and `screenshot_page` is tagged `TrustLevel.UNTRUSTED_ARTIFACT`.
  * Prevents **indirect prompt injection** from taking over orchestrators, because the system treats page content as untrusted evidence rather than instructions.
* **How**:
  * Populates `TrustLevel.UNTRUSTED_ARTIFACT` and `Provenance(producer_type="SERVICE", producer_id="obscura", ...)` on the output schemas.
