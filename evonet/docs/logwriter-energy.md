# Evonet GUI log flusher — energy optimization & macOS verification

## Change summary

`internal/gui/logwriter.go` previously ran a background goroutine that armed
`time.NewTicker(100 * time.Millisecond)`. Every wakeup locked the buffer,
checked a `dirty` flag and — while the app was idle — did nothing. That is
≈10 timer wakeups per second for the entire lifetime of the window, whether or
not any log lines were produced.

The flusher is now event-driven and coalescing (`internal/gui/logbuffer.go`):

* **Idle**: the goroutine blocks on a notification channel with **no armed
  timer**, so an idle window produces **no wakeups at all**.
* **Active**: a write signals the flusher; the goroutine waits out one
  coalescing window (`flushInterval = 100 ms`) and then hands the accumulated
  text to the widget in a single update. The window is not extended by later
  writes, so the maximum UI latency is the same as the previous 100 ms ticker.

macOS energy accounting penalises periodic timer wakeups because they keep the
CPU out of its deep idle states. Removing the fixed 10 Hz ticker removes the
idle wakeups that made the app appear as *"using significant energy"*.

### Files

| File | Role |
| --- | --- |
| `internal/gui/logbuffer.go` | New. Pure, GUI-free `logBuffer` + `logFlusher`; unit-tested on any platform. |
| `internal/gui/logwriter.go` | Fyne adapter; mirrors stderr, batches via the buffer, updates the widget through `fyne.Do`. |
| `internal/gui/logbuffer_test.go` | Tests for idle behaviour, coalescing, ordering, the 1000-line cap, Clear and shutdown. |

### Explicitly preserved behaviour

stderr mirroring, line ordering, the 1,000-line cap, `fyne.Do`
main-goroutine dispatch, the Clear / start-stop / server-change / close
lifecycle, and all WebSocket semantics (untouched).

---

## macOS measurement procedure (before vs. after)

Run this on a Mac to obtain the CPU / Energy Impact numbers that cannot be
produced from a Linux CI host.

### 0. Prerequisites

* macOS with the Xcode command line tools.
* Two checkouts: the commit **before** this change and the commit **after**.
* Do the runs on battery, with Low Power Mode **off**, closing other
  heavyweight apps so the readings are comparable.

### 1. Build the `.app`

```sh
cd evonet
make build-gui-macos          # produces dist/Evonet.app (needs the `fyne` CLI)
```

### 2. Idle measurement — not connected

Launch `Evonet.app`, leave it on the connector view for **3 minutes** without
connecting. In **Activity Monitor → Energy** (enable the extra columns via
*View → Columns*), record for the Evonet process:

* **Energy Impact**
* **% CPU**
* **Idle Wakeups**

### 3. Idle measurement — connected, no traffic

Connect to a server but generate no executor activity, wait 3 minutes, and
record the same three numbers.

### 4. Command-line corroboration

```sh
# Stack sample of the live process. `sample <pid> <seconds>` records where the
# process spends time; inspect the per-thread sample counts / call graph in the
# output file. With the fixed 100 ms ticker removed, the log-flusher goroutine
# should no longer show a steady ~10 Hz wakeup pattern while the app is idle.
PID="$(pgrep -x Evonet)"
sample "$PID" 10 -file /tmp/evonet-sample.txt

# Per-process CPU + wakeups sampled once a second for 30 s
sudo powermetrics --samplers tasks -i 1000 -n 30 | grep -i -A2 -e evonet -e Evonet

# Single-shot summary
top -l 30 -pid "$(pgrep -x Evonet)" -stats pid,cpu,power,idlew

# Where the process wakes (30 s window)
sudo dtrace -n 'profile-97 /pid==$target/ { @[probefunc]=count() }' \
     -p "$(pgrep -x Evonet)" -c 'sleep 30'
```

Record the `power` and `idlew` columns / the wakeup histogram.

### 5. Burst behaviour (functional regression check)

* Trigger a burst of logs (connect, run a command that prints many lines).
* Confirm lines appear promptly (≤ ~100 ms) and in order.
* Confirm the 1,000-line cap holds under a flood.
* Confirm the **Clear** button blanks the view, and that switching servers and
  closing the window do not hang.

### 6. Compare

| Metric | Before | After | Expected |
| --- | --- | --- | --- |
| Idle % CPU | … | … | lower |
| Idle wakeups / s | ≈10+ | … | ≈0 |
| Energy Impact | … | … | lower / no "significant energy" |
| Burst latency | ≤100 ms | … | ≤100 ms (unchanged) |

**Acceptance**: idle wakeups drop to ≈0 and idle CPU / Energy Impact decrease,
with no functional regression in burst latency, ordering, the line cap, Clear,
or shutdown.

---

## Environment limitation (as executed)

This change was implemented and validated on a **Linux** host. The Evonet GUI
compiles only natively on macOS/Windows (build tag
`(windows || darwin) && !headless`), and Fyne's desktop driver requires CGo —
building the GUI for `darwin`/`windows` from Linux fails inside a Fyne
dependency (`github.com/go-gl/gl/v2.1/gl`: *"build constraints exclude all Go
files"*). The repository's own cross-builds therefore use `-tags headless`,
which selects the no-op `gui_stub.go` and never compiles `logwriter.go`.

Consequently:

* The pure logic (`logbuffer.go`) is unit-tested and race-clean on Linux and is
  exercised by CI (`go test ./...`).
* The Linux / macOS / Windows **headless** build matrix is verified green.
* The macOS **before/after Energy Impact numbers could not be captured in this
  environment** because no Mac was available; the procedure above must be run on
  a Mac. The Fyne API calls in `logwriter.go` are unchanged from the
  previously-shipping code, so the adapter itself introduces no new Fyne usage.
