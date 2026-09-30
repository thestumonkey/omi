import Foundation

/// Server addresses chosen in Settings → AI & Automation → Self-hosted Server. [fork-only]
///
/// The app reads its backend URLs from environment values (`OMI_PYTHON_API_URL`,
/// `OMI_DESKTOP_API_URL`, ...) all over, and caches some of them. So a choice made
/// in Settings is saved here and written into the environment at the next launch,
/// by `BundleEnvironment.loadIfNeeded()`, after the `.env` files. It wins over them.
enum SelfHostedSettings {
  /// The main Omi backend. Also serves sign-in and the Firebase REST stand-in.
  static let omiBackendURLKey = "selfHosted.omiBackendURL"
  /// The desktop backend (chat, proactive assistants).
  static let desktopBackendURLKey = "selfHosted.desktopBackendURL"

  /// True when this build signs in against a self-hosted server (Casdoor).
  static var isEnabled: Bool { SelfHostedFirebaseREST.url("") != nil }

  static func savedURL(_ key: String, defaults: UserDefaults = .standard) -> String? {
    normalized(defaults.string(forKey: key))
  }

  /// Saves `raw`, or clears the choice when it is empty. Returns false for text that is not
  /// an http(s) URL, and saves nothing then.
  @discardableResult
  static func save(_ raw: String, for key: String, defaults: UserDefaults = .standard) -> Bool {
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty {
      defaults.removeObject(forKey: key)
      return true
    }
    guard let url = normalized(trimmed) else { return false }
    defaults.set(url, forKey: key)
    return true
  }

  /// The address in use now: from this launch's environment.
  static func currentURL(_ key: String) -> String? {
    let envKey = key == omiBackendURLKey ? "OMI_PYTHON_API_URL" : "OMI_DESKTOP_API_URL"
    return normalized(getenv(envKey).map { String(cString: $0) })
  }

  /// Writes saved choices into the process environment. Called once at launch.
  static func applyOverrides(defaults: UserDefaults = .standard) {
    if let url = savedURL(omiBackendURLKey, defaults: defaults) {
      for envKey in ["OMI_PYTHON_API_URL", "OMI_AUTH_API_URL", SelfHostedFirebaseREST.baseURLKey] {
        setenv(envKey, url, 1)
      }
      log("SelfHostedSettings: Omi backend from Settings: \(url)")
    }
    if let url = savedURL(desktopBackendURLKey, defaults: defaults) {
      setenv("OMI_DESKTOP_API_URL", url, 1)
      log("SelfHostedSettings: desktop backend from Settings: \(url)")
    }
  }

  static func normalized(_ raw: String?) -> String? {
    guard let raw else { return nil }
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard let url = URL(string: trimmed), let scheme = url.scheme?.lowercased(),
      ["http", "https"].contains(scheme), url.host?.isEmpty == false
    else { return nil }
    return trimmed.hasSuffix("/") ? trimmed : trimmed + "/"
  }
}
