import XCTest

@testable import Omi_Computer

final class AssistantPhaseTests: XCTestCase {

  private let defaultInterval: TimeInterval = 600

  /// The whole point. Two equal offsets silently re-create the thundering herd, and
  /// nothing else in the system would catch it.
  func testOffsetsAreDistinct() {
    let offsets = [AssistantPhase.memory, AssistantPhase.task, AssistantPhase.insight]
    XCTAssertEqual(Set(offsets).count, offsets.count, "phase offsets must all differ")
  }

  /// Offsets must fit inside the shared interval, otherwise clamping collapses two of
  /// them onto the same effective phase.
  func testOffsetsFitWithinDefaultInterval() {
    for offset in [AssistantPhase.memory, AssistantPhase.task, AssistantPhase.insight] {
      XCTAssertLessThan(offset, defaultInterval)
      XCTAssertGreaterThanOrEqual(offset, 0)
    }
  }

  /// Seeding must make the first run land exactly `offset` from now.
  func testSeedMakesFirstRunLandAtOffset() {
    let now = Date()
    for offset in [0.0, 200.0, 400.0] {
      let seeded = AssistantPhase.seededLastAnalysisTime(
        offset: offset, interval: defaultInterval, now: now)
      // processLoop computes: remaining = interval - (now - lastAnalysisTime)
      let elapsed = now.timeIntervalSince(seeded)
      let remaining = defaultInterval - elapsed
      XCTAssertEqual(remaining, offset, accuracy: 0.001)
    }
  }

  /// Memory keeps its previous behaviour: fires immediately on the first frame.
  func testMemoryStillFiresImmediatelyAtLaunch() {
    let now = Date()
    let seeded = AssistantPhase.seededLastAnalysisTime(
      offset: AssistantPhase.memory, interval: defaultInterval, now: now)
    let remaining = defaultInterval - now.timeIntervalSince(seeded)
    XCTAssertEqual(remaining, 0, accuracy: 0.001, "memory must not be delayed at launch")
  }

  /// A user shortening the interval below an offset must not delay the first run by
  /// more than one cycle.
  func testOffsetClampedToShortenedInterval() {
    XCTAssertEqual(AssistantPhase.firstRunDelay(offset: 400, interval: 60), 60)
    XCTAssertEqual(AssistantPhase.firstRunDelay(offset: 400, interval: 600), 400)
    XCTAssertEqual(AssistantPhase.firstRunDelay(offset: -5, interval: 600), 0)
    XCTAssertEqual(AssistantPhase.firstRunDelay(offset: 200, interval: 0), 0)
  }

  /// The durability property: because each assistant re-arms from its own last run,
  /// the spacing set at startup must hold for the whole session — never colliding.
  func testStaggerPersistsAcrossManyCycles() {
    let now = Date()
    let assistants: [(String, TimeInterval)] = [
      ("memory", AssistantPhase.memory),
      ("task", AssistantPhase.task),
      ("insight", AssistantPhase.insight),
    ]

    // Fire times over 24 cycles (~4 hours at the 600s default).
    var fires: [(String, TimeInterval)] = []
    for (name, offset) in assistants {
      let first = AssistantPhase.firstRunDelay(offset: offset, interval: defaultInterval)
      for cycle in 0..<24 {
        fires.append((name, first + Double(cycle) * defaultInterval))
      }
    }

    // No two different assistants may ever fire within 60s of each other.
    for a in fires {
      for b in fires where a.0 != b.0 {
        XCTAssertGreaterThan(
          abs(a.1 - b.1), 60,
          "\(a.0) and \(b.0) collide at \(a.1)s / \(b.1)s — stagger did not hold")
      }
    }
    XCTAssertEqual(now, now)  // silence unused warning
  }

  /// Sanity: the spacing is actually spread across the interval rather than clustered
  /// at one end, so no pair sits needlessly close.
  func testOffsetsAreReasonablySpread() {
    let sorted = [AssistantPhase.memory, AssistantPhase.task, AssistantPhase.insight].sorted()
    for (a, b) in zip(sorted, sorted.dropFirst()) {
      XCTAssertGreaterThanOrEqual(b - a, 120, "offsets \(a) and \(b) are too close together")
    }
  }
}
