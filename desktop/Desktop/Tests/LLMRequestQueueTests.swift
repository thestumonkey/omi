import XCTest

@testable import Omi_Computer

/// Tracks how many operations were ever in flight simultaneously.
private actor ConcurrencyProbe {
  private(set) var current = 0
  private(set) var peak = 0
  private(set) var order: [String] = []

  func enter(_ label: String) {
    current += 1
    peak = max(peak, current)
    order.append(label)
  }
  func exit() { current -= 1 }
}

final class LLMRequestQueueTests: XCTestCase {

  /// The core guarantee: with one slot, ten concurrent callers never overlap.
  /// This is the behaviour that was missing — assistants previously all fired at once.
  func testSingleSlotSerializesConcurrentCallers() async throws {
    let queue = LLMRequestQueue(maxConcurrent: 1, maxQueueDepth: 100)
    let probe = ConcurrencyProbe()

    await withTaskGroup(of: Void.self) { group in
      for i in 0..<10 {
        group.addTask {
          try? await queue.run(priority: .background, label: "op\(i)") {
            await probe.enter("op\(i)")
            try? await Task.sleep(nanoseconds: 10_000_000)  // 10ms
            await probe.exit()
          }
        }
      }
    }

    let peak = await probe.peak
    let count = await probe.order.count
    XCTAssertEqual(peak, 1, "single-slot queue must never run two operations at once")
    XCTAssertEqual(count, 10, "every queued operation must eventually run")
  }

  /// Raising the limit actually parallelises, so the gate is a limiter and not a lock.
  func testRespectsConfiguredConcurrency() async throws {
    let queue = LLMRequestQueue(maxConcurrent: 3, maxQueueDepth: 100)
    let probe = ConcurrencyProbe()

    await withTaskGroup(of: Void.self) { group in
      for i in 0..<12 {
        group.addTask {
          try? await queue.run(priority: .background, label: "op\(i)") {
            await probe.enter("op\(i)")
            try? await Task.sleep(nanoseconds: 20_000_000)
            await probe.exit()
          }
        }
      }
    }

    let peak = await probe.peak
    XCTAssertLessThanOrEqual(peak, 3)
    XCTAssertGreaterThan(peak, 1, "should genuinely run in parallel up to the limit")
  }

  /// Interactive work must overtake queued background work — otherwise a user's
  /// chat message waits behind a backlog of screenshot analyses.
  func testInteractiveOvertakesQueuedBackground() async throws {
    let queue = LLMRequestQueue(maxConcurrent: 1, maxQueueDepth: 100)
    let probe = ConcurrencyProbe()
    let occupied = XCTestExpectation(description: "slot occupied")

    // Occupy the only slot.
    let blocker = Task {
      try? await queue.run(priority: .background, label: "blocker") {
        occupied.fulfill()
        try? await Task.sleep(nanoseconds: 200_000_000)  // 200ms
      }
    }
    await fulfillment(of: [occupied], timeout: 2)

    // Queue background first, then interactive.
    let bg = Task {
      try? await queue.run(priority: .background, label: "background") {
        await probe.enter("background")
        await probe.exit()
      }
    }
    try await Task.sleep(nanoseconds: 30_000_000)
    let interactive = Task {
      try? await queue.run(priority: .interactive, label: "interactive") {
        await probe.enter("interactive")
        await probe.exit()
      }
    }

    _ = await blocker.value
    _ = await bg.value
    _ = await interactive.value

    let order = await probe.order
    XCTAssertEqual(
      order, ["interactive", "background"],
      "interactive must jump the queue even though it arrived second")
  }

  /// PTT has a 2s budget and cancels aggressively. A cancelled waiter must leave
  /// the queue and must not leak the slot it never held.
  func testCancelledWaiterReleasesAndDoesNotLeakSlot() async throws {
    let queue = LLMRequestQueue(maxConcurrent: 1, maxQueueDepth: 100)
    let occupied = XCTestExpectation(description: "slot occupied")

    let blocker = Task {
      try? await queue.run(priority: .background, label: "blocker") {
        occupied.fulfill()
        try? await Task.sleep(nanoseconds: 150_000_000)
      }
    }
    await fulfillment(of: [occupied], timeout: 2)

    let doomed = Task {
      try await queue.run(priority: .background, label: "doomed") {
        XCTFail("cancelled waiter must never run its operation")
      }
    }
    try await Task.sleep(nanoseconds: 30_000_000)
    doomed.cancel()

    do {
      try await doomed.value
      XCTFail("cancelled waiter should throw")
    } catch {
      // expected
    }
    _ = await blocker.value

    // The slot must be fully free afterwards: a fresh request runs immediately.
    let ran = XCTestExpectation(description: "slot reusable")
    try await queue.run(priority: .background, label: "after") { ran.fulfill() }
    await fulfillment(of: [ran], timeout: 2)

    let stats = await queue.stats()
    XCTAssertEqual(stats.inFlight, 0, "slot leaked after cancellation")
    XCTAssertEqual(stats.queued, 0)
  }

  // MARK: - Admission policy

  /// Interactive work must get in even when the queue is at its depth cap — a human
  /// is waiting and there is no next tick that would regenerate it.
  func testInteractiveAdmittedWhenQueueIsFull() async throws {
    let queue = LLMRequestQueue(maxConcurrent: 1, maxQueueDepth: 3)
    let occupied = XCTestExpectation(description: "slot occupied")
    let ran = XCTestExpectation(description: "interactive ran")

    let blocker = Task {
      try? await queue.run(priority: .background, label: "blocker") {
        occupied.fulfill()
        try? await Task.sleep(nanoseconds: 250_000_000)
      }
    }
    await fulfillment(of: [occupied], timeout: 2)

    // Fill the queue to its cap with background work.
    let bg = (0..<3).map { i in
      Task { try? await queue.run(priority: .background, label: "bg\(i)") {} }
    }
    try await Task.sleep(nanoseconds: 50_000_000)
    let filled = await queue.stats().queued
    XCTAssertEqual(filled, 3)

    // Arrives into a full queue — must still be admitted.
    let interactive = Task {
      try await queue.run(priority: .interactive, label: "chat") { ran.fulfill() }
    }

    _ = await blocker.value
    await fulfillment(of: [ran], timeout: 3)
    _ = try? await interactive.value
    for t in bg { _ = await t.value }
  }

  /// Admitting the newcomer must cost the *stalest* background waiter, not the newest —
  /// the oldest queued frame is the least valuable one to spend a slot on.
  func testEvictsStalestBackgroundWaiter() async throws {
    let queue = LLMRequestQueue(maxConcurrent: 1, maxQueueDepth: 2)
    let occupied = XCTestExpectation(description: "slot occupied")
    let probe = ConcurrencyProbe()

    let blocker = Task {
      try? await queue.run(priority: .background, label: "blocker") {
        occupied.fulfill()
        try? await Task.sleep(nanoseconds: 300_000_000)
      }
    }
    await fulfillment(of: [occupied], timeout: 2)

    // "oldest" queues first, so it accrues the longest wait.
    let oldest = Task {
      try await queue.run(priority: .background, label: "oldest") {
        await probe.enter("oldest")
      }
    }
    try await Task.sleep(nanoseconds: 60_000_000)
    let newer = Task {
      try await queue.run(priority: .background, label: "newer") {
        await probe.enter("newer")
      }
    }
    try await Task.sleep(nanoseconds: 60_000_000)
    let atCap = await queue.stats().queued
    XCTAssertEqual(atCap, 2, "queue should be at its cap")

    // Third arrival forces an eviction.
    let freshest = Task {
      try await queue.run(priority: .background, label: "freshest") {
        await probe.enter("freshest")
      }
    }

    _ = await blocker.value
    let oldestResult = await oldest.result
    _ = await newer.result
    _ = await freshest.result

    // The stalest waiter was shed...
    switch oldestResult {
    case .failure(let error):
      guard case LLMRequestQueue.QueueError.shed = error else {
        return XCTFail("expected shed, got \(error)")
      }
    case .success:
      XCTFail("stalest waiter should have been evicted")
    }

    // ...and never ran, while the two fresher requests did.
    let order = await probe.order
    XCTAssertFalse(order.contains("oldest"), "evicted waiter must not run")
    XCTAssertTrue(order.contains("newer"))
    XCTAssertTrue(order.contains("freshest"))
    let shed = await queue.stats().shedCount
    XCTAssertEqual(shed, 1)
  }

  /// With nothing droppable (every waiter interactive), a background newcomer is
  /// rejected rather than displacing work a human is waiting on.
  func testBackgroundRejectedWhenQueueIsAllInteractive() async throws {
    let queue = LLMRequestQueue(maxConcurrent: 1, maxQueueDepth: 2)
    let occupied = XCTestExpectation(description: "slot occupied")

    let blocker = Task {
      try? await queue.run(priority: .background, label: "blocker") {
        occupied.fulfill()
        try? await Task.sleep(nanoseconds: 250_000_000)
      }
    }
    await fulfillment(of: [occupied], timeout: 2)

    let interactive = (0..<2).map { i in
      Task { try? await queue.run(priority: .interactive, label: "chat\(i)") {} }
    }
    try await Task.sleep(nanoseconds: 50_000_000)

    let rejected = Task {
      try await queue.run(priority: .background, label: "bg") {
        XCTFail("rejected request must not run")
      }
    }

    switch await rejected.result {
    case .failure(let error):
      guard case LLMRequestQueue.QueueError.shed = error else {
        return XCTFail("expected shed, got \(error)")
      }
    case .success:
      XCTFail("background should be rejected when nothing is droppable")
    }

    _ = await blocker.value
    for t in interactive { _ = await t.value }
  }

  /// Queue depth must be observable — this is the "representation on the desktop
  /// side" that was missing while requests piled up invisibly.
  func testStatsReportQueueDepth() async throws {
    let queue = LLMRequestQueue(maxConcurrent: 1, maxQueueDepth: 100)
    let occupied = XCTestExpectation(description: "slot occupied")

    let blocker = Task {
      try? await queue.run(priority: .background, label: "blocker") {
        occupied.fulfill()
        try? await Task.sleep(nanoseconds: 200_000_000)
      }
    }
    await fulfillment(of: [occupied], timeout: 2)

    let waiters = (0..<4).map { i in
      Task { try? await queue.run(priority: .background, label: "w\(i)") {} }
    }
    try await Task.sleep(nanoseconds: 50_000_000)

    let mid = await queue.stats()
    XCTAssertEqual(mid.inFlight, 1)
    XCTAssertEqual(mid.queued, 4, "queued work must be countable, not invisible")
    XCTAssertGreaterThan(mid.oldestWaitSeconds, 0)

    _ = await blocker.value
    for w in waiters { _ = await w.value }
  }
}
