import OmiTheme
import SwiftUI

// Settings → AI & Automation → Self-hosted Server. [fork-only]
//
// Server addresses (applied at the next launch, see `SelfHostedSettings`), the
// chat model the self-hosted server uses (server-wide, applied at once), and
// the cloud speech-to-text switch.

extension SettingsContentView {
  var selfHostedSubsection: some View {
    settingsCard(settingId: "selfhosted.server") {
      SelfHostedServerSettingsView()
    }
  }
}

private struct SelfHostedLLMSettings: Codable {
  let model: String
  let defaultModel: String
  let models: [String]
  let modelsError: String?

  enum CodingKeys: String, CodingKey {
    case model, models
    case defaultModel = "default_model"
    case modelsError = "models_error"
  }
}

private struct SelfHostedLLMUpdate: Encodable {
  let model: String?
}

struct SelfHostedServerSettingsView: View {
  @State private var omiURL = SelfHostedSettings.savedURL(SelfHostedSettings.omiBackendURLKey) ?? ""
  @State private var desktopURL = SelfHostedSettings.savedURL(SelfHostedSettings.desktopBackendURLKey) ?? ""
  @State private var urlMessage: String?
  @State private var urlMessageIsError = false

  @State private var llm: SelfHostedLLMSettings?
  /// "" = the server's default model.
  @State private var selectedModel = ""
  @State private var modelMessage: String?
  @State private var isSavingModel = false

  @AppStorage("forceCloudSTT") private var forceCloudSTT = false

  var body: some View {
    VStack(alignment: .leading, spacing: OmiSpacing.lg) {
      urlFields
      Divider()
      modelPicker
      Divider()
      Toggle(isOn: $forceCloudSTT) {
        VStack(alignment: .leading, spacing: 2) {
          Text("Server speech-to-text")
            .scaledFont(size: OmiType.subheading, weight: .semibold)
            .foregroundColor(Ink.primary)
          Text(
            "Send audio to the server for transcription instead of the on-device model. Starts with the next recording."
          )
          .scaledFont(size: OmiType.caption)
          .foregroundColor(Ink.secondary)
        }
      }
      .toggleStyle(.switch)
    }
    .task { await loadModels() }
  }

  // MARK: Server addresses

  private var urlFields: some View {
    VStack(alignment: .leading, spacing: OmiSpacing.md) {
      Text("Server addresses")
        .scaledFont(size: OmiType.subheading, weight: .semibold)
        .foregroundColor(Ink.primary)
      urlField(
        "Omi backend", text: $omiURL, key: SelfHostedSettings.omiBackendURLKey,
        help: "Sign-in, conversations, memories.")
      urlField(
        "Desktop backend", text: $desktopURL, key: SelfHostedSettings.desktopBackendURLKey,
        help: "Chat and assistants.")
      HStack {
        Button("Save addresses") { saveURLs() }
        if let urlMessage {
          Text(urlMessage)
            .scaledFont(size: OmiType.caption)
            .foregroundColor(urlMessageIsError ? Ink.errorRed : Ink.secondary)
        }
      }
    }
  }

  private func urlField(_ title: String, text: Binding<String>, key: String, help: String) -> some View {
    VStack(alignment: .leading, spacing: 4) {
      Text(title).scaledFont(size: OmiType.body).foregroundColor(Ink.primary)
      TextField(SelfHostedSettings.currentURL(key) ?? "https://", text: text)
        .textFieldStyle(.roundedBorder)
        .autocorrectionDisabled()
      Text("\(help) Empty = use the built-in address (\(SelfHostedSettings.currentURL(key) ?? "none")).")
        .scaledFont(size: OmiType.caption)
        .foregroundColor(Ink.secondary)
    }
  }

  private func saveURLs() {
    let okOmi = SelfHostedSettings.save(omiURL, for: SelfHostedSettings.omiBackendURLKey)
    let okDesktop = SelfHostedSettings.save(desktopURL, for: SelfHostedSettings.desktopBackendURLKey)
    if okOmi && okDesktop {
      urlMessageIsError = false
      urlMessage = "Saved. Quit and reopen Omi to use them."
    } else {
      urlMessageIsError = true
      urlMessage = "Use a full address, like https://omi.example.ts.net/"
    }
  }

  // MARK: Chat model

  private var modelPicker: some View {
    VStack(alignment: .leading, spacing: OmiSpacing.sm) {
      HStack {
        Text("AI model")
          .scaledFont(size: OmiType.subheading, weight: .semibold)
          .foregroundColor(Ink.primary)
        Spacer()
        if let llm {
          SettingsMenuPicker(selection: $selectedModel) {
            Text("Default (\(llm.defaultModel))").tag("")
            ForEach(llm.models.filter { $0 != llm.defaultModel }, id: \.self) { Text($0).tag($0) }
          }
          .disabled(isSavingModel)
          .onChange(of: selectedModel) { _, newValue in
            Task { await saveModel(newValue) }
          }
        } else {
          ProgressView().scaleEffect(0.6)
        }
      }
      Text(
        modelMessage
          ?? "The model the server uses for summaries, memories and chat. Applies to all devices within 30 seconds."
      )
      .scaledFont(size: OmiType.caption)
      .foregroundColor(Ink.secondary)
    }
  }

  private func apply(_ settings: SelfHostedLLMSettings) {
    llm = settings
    let current = settings.model == settings.defaultModel ? "" : settings.model
    if selectedModel != current { selectedModel = current }
    modelMessage = settings.modelsError
  }

  private func loadModels() async {
    do {
      let settings: SelfHostedLLMSettings = try await APIClient.shared.get("v1/selfhosted/llm")
      apply(settings)
    } catch {
      modelMessage = "Could not load the model list: \(error.localizedDescription)"
    }
  }

  private func saveModel(_ model: String) async {
    guard let llm, model != (llm.model == llm.defaultModel ? "" : llm.model) else { return }
    isSavingModel = true
    defer { isSavingModel = false }
    do {
      let settings: SelfHostedLLMSettings = try await APIClient.shared.post(
        "v1/selfhosted/llm", body: SelfHostedLLMUpdate(model: model.isEmpty ? nil : model))
      apply(settings)
    } catch {
      modelMessage = "Could not change the model: \(error.localizedDescription)"
    }
  }
}
