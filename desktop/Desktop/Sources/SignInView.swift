import SwiftUI

struct SignInView: View {
    @ObservedObject var authState: AuthState

    var body: some View {
        ZStack {
            // Full background
            OmiColors.backgroundPrimary
                .ignoresSafeArea()

            // Centered sign in card
            VStack(spacing: 32) {
                Spacer()

                // Logo/Title
                VStack(spacing: 16) {
                    // Omi logo
                    if let logoURL = Bundle.resourceBundle.url(forResource: "herologo", withExtension: "png"),
                       let logoImage = NSImage(contentsOf: logoURL) {
                        Image(nsImage: logoImage)
                            .resizable()
                            .aspectRatio(contentMode: .fit)
                            .frame(width: 64, height: 64)
                    }

                    Text("omi")
                        .scaledFont(size: 48, weight: .bold)
                        .foregroundColor(OmiColors.textPrimary)

                    Text("Sign in to continue")
                        .font(.title3)
                        .foregroundColor(OmiColors.textTertiary)
                }

                Spacer()

                // Sign in button — single Casdoor (OIDC) flow. Both the old
                // Apple/Google buttons routed here anyway; the provider hint is
                // not forwarded to Casdoor, which presents its own login page.
                VStack(spacing: 12) {
                    Button(action: {
                        Task {
                            do {
                                try await AuthService.shared.signInWithGoogle()
                            } catch is CancellationError {
                                // swallow — user initiated
                            } catch AuthError.cancelled {
                                // swallow — user initiated
                            } catch {
                                let errorMsg = "Error: \(error.localizedDescription)"
                                authState.error = errorMsg
                                NSLog("OMI Sign in error: %@", errorMsg)
                            }
                        }
                    }) {
                        HStack(spacing: 8) {
                            Image(systemName: "lock.shield.fill")
                                .scaledFont(size: 18)
                            Text("Continue with Casdoor")
                                .scaledFont(size: 17, weight: .medium)
                        }
                        .foregroundColor(.white)
                        .frame(maxWidth: .infinity)
                        .frame(height: 50)
                        .background(Color(red: 0.086, green: 0.467, blue: 1.0)) // Casdoor blue #1677FF
                        .cornerRadius(10)
                    }
                    .buttonStyle(.plain)
                    .disabled(authState.isLoading)

                    // Loading overlay for both buttons
                    if authState.isLoading {
                        ProgressView()
                            .progressViewStyle(CircularProgressViewStyle(tint: OmiColors.textPrimary))
                            .padding(.top, 8)

                        // Minimal escape hatch so a failed web sign-in (closed tab,
                        // denied on Apple/Google, etc.) doesn't trap the user with
                        // permanently disabled buttons waiting for a callback that
                        // will never arrive.
                        Button(action: {
                            AuthService.shared.cancelSignIn()
                        }) {
                            Text("Cancel")
                                .font(.caption)
                                .foregroundColor(OmiColors.textTertiary)
                        }
                        .buttonStyle(.plain)
                        .padding(.top, 4)
                    }

                    if let error = authState.error {
                        Text(error)
                            .font(.caption)
                            .foregroundColor(OmiColors.error)
                            .multilineTextAlignment(.center)
                            .padding(.top, 4)
                    }
                }
                .frame(width: 320)

                Spacer()
                    .frame(height: 60)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

// MARK: - Google Logo

/// Standard multicolor Google "G" logo
struct GoogleLogo: View {
    var body: some View {
        if let url = Bundle.resourceBundle.url(forResource: "google_logo", withExtension: "png"),
           let image = NSImage(contentsOf: url) {
            Image(nsImage: image)
                .resizable()
                .aspectRatio(contentMode: .fit)
        }
    }
}

#Preview {
    SignInView(authState: AuthState.shared)
}
