import Foundation

/// Startup phase offsets that keep the periodic extractors from converging.
///
/// Memory, Task and Insight all default to a 600s extraction interval. Identical
/// periods started at the same moment never drift apart: without an offset all three
/// fire on the first frame after launch and then stay in lockstep forever, producing
/// a burst of simultaneous LLM calls every 10 minutes. Against a backend serving with
/// `--parallel 1` that burst serialises into one long queue instead of overlapping.
///
/// Offsetting the *phase* rather than the *period* keeps each assistant's own cadence
/// (and the user's configured interval) exactly as it was — it only changes where in
/// the cycle each one sits. Because every assistant then re-arms from its own last run,
/// the spacing established at startup persists for the lifetime of the process.
///
/// Keep these values distinct and spread across the shared 600s default. Setting two
/// of them equal silently re-creates the thundering herd.
enum AssistantPhase {
  /// Fires promptly at launch — the primary extractor stays as responsive as before.
  static let memory: TimeInterval = 0
  static let task: TimeInterval = 200
  static let insight: TimeInterval = 400

  /// Delay before this assistant's first run.
  ///
  /// Clamped to the interval so that shortening the extraction interval in settings
  /// can never cause the offset to delay the first run by *more* than one full cycle.
  static func firstRunDelay(offset: TimeInterval, interval: TimeInterval) -> TimeInterval {
    min(max(0, offset), max(0, interval))
  }

  /// Seed value for an assistant's `lastAnalysisTime` that makes its first run land
  /// `offset` seconds from now, while leaving every subsequent run a full `interval`
  /// apart.
  static func seededLastAnalysisTime(
    offset: TimeInterval, interval: TimeInterval, now: Date = Date()
  ) -> Date {
    now.addingTimeInterval(firstRunDelay(offset: offset, interval: interval) - interval)
  }
}
