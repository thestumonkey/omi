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
