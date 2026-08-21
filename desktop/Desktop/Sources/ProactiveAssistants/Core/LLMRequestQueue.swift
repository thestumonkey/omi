import Foundation

/// Global admission gate for every outbound LLM call.
///
/// Why this exists: the app runs ~10 independent proactive assistants, each on its
/// own timer with its own single-frame coalescer. Individually each is serial, but
/// collectively they fan out — when a backlog builds, every assistant fires at once.
/// A self-hosted backend serving with `--parallel 1` has exactly one slot, so those
/// concurrent requests don't parallelise: they pile into an unbounded, unobservable
/// queue behind whichever request happens to be running.
///
/// This actor puts the queue on *our* side of the wire, where we can bound it,
/// prioritise it, and see it.
///
/// Usage — wrap the network call only, never the retry backoff:
/// ```swift
/// let (data, response) = try await LLMRequestQueue.shared.run(
///   priority: .background, label: "memory.extract"
/// ) {
///   try await URLSession.shared.data(for: urlRequest)
/// }
/// ```
actor LLMRequestQueue {
  static let shared = LLMRequestQueue()

  // MARK: - Priority

  /// Interactive work has a human waiting on it and must overtake background work.
  /// Without this, a backlog of 200 queued screenshot analyses would make the user's
  /// chat message wait behind all of them.
  enum Priority: Int, Comparable, Sendable {
    case background = 0  // screenshot analysis, memory/insight/task extraction, embeddings
    case interactive = 1  // chat, push-to-talk, onboarding — user is watching a spinner

    static func < (lhs: Priority, rhs: Priority) -> Bool { lhs.rawValue < rhs.rawValue }
  }

  enum QueueError: LocalizedError {
    case shed(label: String, depth: Int)

    var errorDescription: String? {
      switch self {
      case .shed(_, let depth):
        return "AI service is saturated (\(depth) requests queued). Skipped."
      }
    }
  }

  /// What to do with an incoming request when the queue is already full.
  enum Admission {
    /// Accept it onto the queue.
    case enqueue
    /// Reject the incoming request immediately with `QueueError.shed`.
    case reject
    /// Drop the single lowest-value waiter already queued, then accept this one.
    /// The evicted waiter fails with `QueueError.shed`.
    case evictThenEnqueue(victim: UUID)
  }

  // MARK: - Configuration

  /// Concurrent in-flight requests. Defaults to 1 to match a self-hosted
  /// `--parallel 1` backend; override with `OMI_LLM_MAX_CONCURRENT` when pointing
  /// at an elastic endpoint that genuinely benefits from parallelism.
  private let maxConcurrent: Int

  /// Waiters allowed to accumulate before `admit(...)` starts shedding.
  private let maxQueueDepth: Int

  /// Log a warning when a request sat in the queue longer than this.
  private static let slowWaitThreshold: TimeInterval = 30

  // MARK: - State

  private struct Waiter {
    let id: UUID
    let priority: Priority
    let seq: UInt64
    let label: String
    let enqueuedAt: Date
    let continuation: CheckedContinuation<Void, Error>
  }

  private var inFlight = 0
  private var waiters: [Waiter] = []
  private var seqCounter: UInt64 = 0
  /// Ids cancelled before their waiter was recorded (cancellation/enqueue race).
  private var preCancelled: Set<UUID> = []

  private var shedCount = 0
  private var peakDepth = 0

  init(maxConcurrent: Int? = nil, maxQueueDepth: Int? = nil) {
    func envInt(_ key: String) -> Int? {
      guard let c = getenv(key), let s = String(validatingUTF8: c), let v = Int(s), v > 0 else {
        return nil
      }
      return v
    }
    self.maxConcurrent = maxConcurrent ?? envInt("OMI_LLM_MAX_CONCURRENT") ?? 1
    self.maxQueueDepth = maxQueueDepth ?? envInt("OMI_LLM_MAX_QUEUE_DEPTH") ?? 24
  }

  // MARK: - Public API

  /// Run `operation` once a slot is free. Wrap the network call only — not the
  /// retry backoff, and not request-body construction.
  nonisolated func run<T>(
    priority: Priority = .background,
    label: String,
    operation: () async throws -> T
  ) async throws -> T {
    try await acquire(priority: priority, label: label)
    do {
      let result = try await operation()
      await release()
      return result
    } catch {
      await release()
      throw error
    }
  }

  /// Snapshot for logging and UI. This is the "representation on the desktop side"
  /// that was missing — queued work is now countable instead of invisible.
  struct Stats: Sendable {
    let inFlight: Int
    let queued: Int
    let queuedInteractive: Int
    let oldestWaitSeconds: TimeInterval
    let peakDepth: Int
    let shedCount: Int
    let maxConcurrent: Int
  }

  func stats() -> Stats {
    Stats(
      inFlight: inFlight,
      queued: waiters.count,
      queuedInteractive: waiters.filter { $0.priority == .interactive }.count,
      oldestWaitSeconds: waiters.map { Date().timeIntervalSince($0.enqueuedAt) }.max() ?? 0,
      peakDepth: peakDepth,
      shedCount: shedCount,
      maxConcurrent: maxConcurrent
    )
  }

  // MARK: - Admission policy

  /// Decide what happens to an incoming request when the queue is at capacity.
  ///
  /// This is the policy knob. The trade-offs:
  ///
  /// - **Never shed** (always `.enqueue`) — no work is lost, but the queue grows
  ///   without bound and every request eventually ages past its own 300s client
  ///   timeout. That is the behaviour we are trying to fix.
  /// - **Reject the newcomer** (`.reject`) — protects work already committed to,
  ///   but the *newest* screenshot is the most relevant one; discarding it in
  ///   favour of a 4-minute-old frame means the assistants analyse stale context.
  /// - **Evict an old background waiter** (`.evictThenEnqueue`) — keeps the queue
  ///   fresh and lets interactive work always get in, at the cost of silently
  ///   dropping background work that had already been accepted.
  ///
  /// Relevant context: background assistants already coalesce via `pendingFrame`,
  /// so a superseded background request is genuinely worthless — losing it costs
  /// nothing, and the assistant will re-fire on its next timer tick. Interactive
  /// requests have a human waiting and cannot be silently dropped.
  ///
  /// - Parameters:
  ///   - incoming: priority and label of the request asking to be queued.
  ///   - queued: current waiters, ordered as they will be served (highest priority
  ///     first, then FIFO within a priority).
  private func admit(
    incoming: (priority: Priority, label: String),
    queued: [(id: UUID, priority: Priority, label: String, waitedFor: TimeInterval)]
  ) -> Admission {
    // TODO(stu): implement the admission policy.
    //
    // Sketch of one reasonable shape — adjust to taste:
    //   1. Interactive requests must always get in. If the queue is full, evict the
    //      oldest background waiter to make room.
    //   2. A background request arriving into a full queue is the freshest signal
    //      available, so prefer evicting the oldest background waiter over rejecting it.
    //   3. If every waiter is interactive, there is nothing safe to evict — reject.
    //
    // Return `.enqueue`, `.reject`, or `.evictThenEnqueue(victim: <id>)`.
    return .reject
  }

  // MARK: - Gate mechanics

  private func acquire(priority: Priority, label: String) async throws {
    try Task.checkCancellation()

    if inFlight < maxConcurrent {
      inFlight += 1
      return
    }

    seqCounter += 1
    let id = UUID()
    let seq = seqCounter

    try await withTaskCancellationHandler {
      try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
        // Synchronous, so still on this actor's executor — no interleaving.
        self.enqueue(
          Waiter(
            id: id, priority: priority, seq: seq, label: label,
            enqueuedAt: Date(), continuation: cont
          )
        )
      }
    } onCancel: {
      Task { await self.cancelWaiter(id) }
    }
  }

  private func enqueue(_ waiter: Waiter) {
    // Lost the race with `onCancel` — the task was cancelled before we recorded it.
    if preCancelled.remove(waiter.id) != nil {
      waiter.continuation.resume(throwing: CancellationError())
      return
    }

    if waiters.count >= maxQueueDepth {
      let snapshot = waiters.map {
        (id: $0.id, priority: $0.priority, label: $0.label,
         waitedFor: Date().timeIntervalSince($0.enqueuedAt))
      }
      switch admit(incoming: (waiter.priority, waiter.label), queued: snapshot) {
      case .reject:
        shedCount += 1
        log("LLMQueue: shed \(waiter.label) — queue full (\(waiters.count)/\(maxQueueDepth))")
        waiter.continuation.resume(throwing: QueueError.shed(label: waiter.label, depth: waiters.count))
        return
      case .evictThenEnqueue(let victimID):
        if let idx = waiters.firstIndex(where: { $0.id == victimID }) {
          let victim = waiters.remove(at: idx)
          shedCount += 1
          log("LLMQueue: evicted \(victim.label) to admit \(waiter.label)")
          victim.continuation.resume(throwing: QueueError.shed(label: victim.label, depth: waiters.count))
        }
      case .enqueue:
        break
      }
    }

    // Ordered insert: higher priority first, FIFO within a priority.
    let idx = waiters.firstIndex { $0.priority < waiter.priority } ?? waiters.count
    waiters.insert(waiter, at: idx)
    peakDepth = max(peakDepth, waiters.count)

    if waiters.count == 1 || waiters.count % 5 == 0 {
      log("LLMQueue: \(waiter.label) queued — depth \(waiters.count), in-flight \(inFlight)/\(maxConcurrent)")
    }
  }

  private func cancelWaiter(_ id: UUID) {
    guard let idx = waiters.firstIndex(where: { $0.id == id }) else {
      // Cancellation arrived before enqueue; let enqueue handle it.
      preCancelled.insert(id)
      return
    }
    let waiter = waiters.remove(at: idx)
    waiter.continuation.resume(throwing: CancellationError())
  }

  private func release() {
    // Direct hand-off to the next waiter: `inFlight` stays put, so a freed slot
    // can never be stolen by a newly-arriving request ahead of the queue.
    while let next = waiters.first {
      waiters.removeFirst()
      let waited = Date().timeIntervalSince(next.enqueuedAt)
      if waited > Self.slowWaitThreshold {
        log("LLMQueue: \(next.label) waited \(String(format: "%.1f", waited))s (depth now \(waiters.count))")
      }
      next.continuation.resume()
      return
    }
    inFlight = max(0, inFlight - 1)
  }
}
