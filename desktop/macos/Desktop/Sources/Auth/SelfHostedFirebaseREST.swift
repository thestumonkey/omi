import Foundation

/// Self-hosted redirect for the Firebase REST calls that finish sign-in. [fork-only]
///
/// Sign-in ends with two calls to Google: redeeming the backend's custom token
/// (identitytoolkit `accounts:signInWithCustomToken`) and refreshing the ID
/// token (securetoken `/v1/token`). When `OMI_FIREBASE_REST_BASE_URL` is set
/// (e.g. `https://omi.example.ts.net/`), both go to that server instead, which
/// answers them from its own identity provider in Firebase's format. The path
/// keeps the Google host as its first segment, the same shape the Firebase Auth
/// emulator uses.
enum SelfHostedFirebaseREST {
  static let baseURLKey = "OMI_FIREBASE_REST_BASE_URL"

  /// `hostAndPath` is e.g. `"identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=K"`.
  /// Returns nil when the override is not configured, so callers keep Google.
  static func url(_ hostAndPath: String) -> URL? {
    guard let raw = getenv(baseURLKey).map({ String(cString: $0) }) else { return nil }
    let base = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !base.isEmpty else { return nil }
    return URL(string: (base.hasSuffix("/") ? base : base + "/") + hostAndPath)
  }
}
