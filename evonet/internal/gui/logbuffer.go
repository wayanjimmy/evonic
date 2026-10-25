package gui

import (
	"strings"
	"sync"
	"time"
)

// maxLines is the maximum number of log lines retained by the buffer. Once the
// cap is exceeded the oldest lines are dropped so memory use stays bounded.
const maxLines = 1000

// flushInterval bounds how long a burst of log writes may be coalesced before
// the accumulated text is handed to the display. It matches the cadence of the
// previous fixed ticker (100 ms), so the maximum UI latency is unchanged even
// though no timer runs while the writer is idle.
const flushInterval = 100 * time.Millisecond

// logBuffer is a concurrency-safe, fixed-capacity line buffer. It holds no
// reference to any GUI toolkit, which keeps the batching logic testable on
// every platform, including headless CI.
type logBuffer struct {
	mu    sync.Mutex
	lines []string
	dirty bool
}

// append splits a raw log payload into lines, drops empty ones, stores the
// rest and trims the buffer back to maxLines. It mirrors the line handling of
// the previous implementation so line ordering and the 1000-line cap are
// preserved exactly.
func (b *logBuffer) append(p []byte) {
	incoming := strings.Split(strings.TrimRight(string(p), "\n"), "\n")
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, line := range incoming {
		if line != "" {
			b.lines = append(b.lines, line)
		}
	}
	if len(b.lines) > maxLines {
		b.lines = b.lines[len(b.lines)-maxLines:]
	}
	b.dirty = true
}

// snapshot returns the joined buffer contents and clears the dirty flag. The
// boolean reports whether there was anything new to render; it is false when
// the buffer has not changed since the previous snapshot.
func (b *logBuffer) snapshot() (string, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.dirty {
		return "", false
	}
	text := strings.Join(b.lines, "\n")
	b.dirty = false
	return text, true
}

// clear empties the buffer. The caller is responsible for clearing the
// display; nothing is emitted as a result of clearing.
func (b *logBuffer) clear() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.lines = nil
	b.dirty = false
}

// logFlusher owns the single goroutine that batches buffered lines and passes
// the coalesced text to sink. While there is nothing to render the goroutine
// blocks on a notification channel with no active timer, so an idle writer
// performs no wakeups at all (unlike the previous 100 ms periodic ticker).
type logFlusher struct {
	buf       *logBuffer
	interval  time.Duration
	sink      func(string)
	notify    chan struct{}
	closeCh   chan struct{}
	done      chan struct{}
	closeOnce sync.Once
}

// newLogFlusher creates a flusher and starts its goroutine. sink is invoked
// from the flusher goroutine each time coalesced text is ready.
func newLogFlusher(buf *logBuffer, interval time.Duration, sink func(string)) *logFlusher {
	f := &logFlusher{
		buf:      buf,
		interval: interval,
		sink:     sink,
		notify:   make(chan struct{}, 1),
		closeCh:  make(chan struct{}),
		done:     make(chan struct{}),
	}
	go f.run()
	return f
}

// notifyWrite signals that new data is available. It never blocks: if a flush
// is already pending, an extra notification would be redundant.
func (f *logFlusher) notifyWrite() {
	select {
	case f.notify <- struct{}{}:
	default:
	}
}

// run blocks until there is data to flush, waits out the coalescing window so
// that a burst of writes is rendered in a single update, then repeats. When
// idle it is parked in the first select with no timer scheduled.
func (f *logFlusher) run() {
	defer close(f.done)
	for {
		select {
		case <-f.closeCh:
			return
		case <-f.notify:
		}

		// Wait out the coalescing window. Notifications that arrive in the
		// meantime are simply absorbed here; their data is already in the
		// buffer and will be part of the next snapshot.
		timer := time.NewTimer(f.interval)
		select {
		case <-f.closeCh:
			timer.Stop()
			return
		case <-timer.C:
		}

		f.flush()
	}
}

// flush renders the pending buffer contents, if any.
func (f *logFlusher) flush() {
	text, dirty := f.buf.snapshot()
	if !dirty {
		return
	}
	f.sink(text)
}

// close stops the flusher goroutine and waits for it to exit. It is safe to
// call more than once.
func (f *logFlusher) close() {
	f.closeOnce.Do(func() { close(f.closeCh) })
	<-f.done
}
