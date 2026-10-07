// [fork-only] Recording is paused while a conversation plays back, so the
// pendant does not record the playback from the phone speaker and transcribe
// it into the current conversation.
import 'dart:async';

import 'package:omi/providers/capture_provider.dart';
import 'package:omi/utils/audio/conversation_playback_controller.dart';
import 'package:omi/utils/logger.dart';

/// Follows the detail page's [ConversationPlaybackController] and pauses
/// capture while it reports playing. It resumes capture only when it was the
/// one that paused it, so a capture the user muted stays muted.
class PlaybackCapturePause {
  PlaybackCapturePause(this._playback, this._capture) {
    _playback.addListener(_onPlaybackChanged);
  }

  final ConversationPlaybackController _playback;
  final CaptureProvider? _capture;
  bool _pausedByPlayback = false;

  Future<void> _onPlaybackChanged() async {
    final capture = _capture;
    if (capture == null) return;
    final audible = _playback.isPlaying;
    try {
      if (audible && !_pausedByPlayback && !capture.isPaused) {
        _pausedByPlayback = true;
        await capture.pauseCapture();
      } else if (!audible && _pausedByPlayback) {
        _pausedByPlayback = false;
        await capture.resumeCapture();
      }
    } catch (e) {
      // Nothing was recording (or the source refused): leave capture as it is.
      Logger.debug('Playback capture pause/resume skipped: $e');
      if (audible) _pausedByPlayback = false;
    }
  }

  /// Call before the controller is disposed. The player detaches without a
  /// notification during page teardown, so resume here.
  void dispose() {
    _playback.removeListener(_onPlaybackChanged);
    if (_pausedByPlayback) {
      _pausedByPlayback = false;
      unawaited(_capture?.resumeCapture().catchError((_) {}));
    }
  }
}
