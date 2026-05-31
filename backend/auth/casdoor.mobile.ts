/**
 * Casdoor — mobile auth config
 * ==============================
 * Mobile apps use the OAuth Authorization Code + PKCE flow directly.
 * No server-side session — tokens are stored in device secure storage.
 *
 * Choose one library:
 *
 * A) expo-auth-session  (Expo / React Native managed workflow)
 *    npm install expo-auth-session expo-crypto expo-web-browser
 *    https://docs.expo.dev/versions/latest/sdk/auth-session/
 *
 * B) react-native-app-auth  (bare React Native, supports background refresh)
 *    npm install react-native-app-auth
 *    https://github.com/FormidableLabs/react-native-app-auth
 *
 * Required env vars (embedded at build time via app.config.js / .env):
 *   CASDOOR_EXTERNAL_URL   Public Casdoor URL (browser-facing)
 *   CASDOOR_CLIENT_ID      Written by casdoor-provision
 *   CASDOOR_ORG_NAME       Your org name
 *   CASDOOR_APP_NAME       Your app name
 *
 * Deep link / redirect URI
 * ------------------------
 * Register a custom scheme in your app (e.g. myapp://auth/callback) and
 * add it to config/casdoor/apps.yaml redirectUris before running casdoor-provision.
 *
 * Token storage
 * -------------
 * Always store tokens in secure storage, never AsyncStorage:
 *   expo-secure-store          (Expo)
 *   react-native-keychain      (bare React Native)
 */

import Constants from 'expo-constants'

const CASDOOR_URL  = Constants.expoConfig?.extra?.casdoorUrl  as string
const CLIENT_ID    = Constants.expoConfig?.extra?.clientId    as string
const ORG_NAME     = Constants.expoConfig?.extra?.orgName     as string
const APP_NAME     = Constants.expoConfig?.extra?.appName     as string
const REDIRECT_URI = 'myapp://auth/callback' // ← match your deep link scheme

// ─── Option A: expo-auth-session ─────────────────────────────────────────────

import * as AuthSession  from 'expo-auth-session'
import * as WebBrowser   from 'expo-web-browser'
import * as SecureStore  from 'expo-secure-store'

WebBrowser.maybeCompleteAuthSession()

export const casdoorDiscovery: AuthSession.DiscoveryDocument = {
  authorizationEndpoint: `${CASDOOR_URL}/login/oauth/authorize`,
  tokenEndpoint:         `${CASDOOR_URL}/api/login/oauth/access_token`,
  revocationEndpoint:    `${CASDOOR_URL}/api/login/oauth/revoke_token`,
}

export const casdoorRequest = new AuthSession.AuthRequest({
  clientId:            CLIENT_ID,
  redirectUri:         REDIRECT_URI,
  scopes:              ['openid', 'profile', 'email'],
  usePKCE:             true,
  extraParams: {
    org_name: ORG_NAME,
    app_name: APP_NAME,
  },
})

// Usage in a component:
//
//   const [request, response, promptAsync] = AuthSession.useAuthRequest(
//     { clientId: CLIENT_ID, redirectUri: REDIRECT_URI, scopes: ['openid', 'profile', 'email'], usePKCE: true },
//     casdoorDiscovery,
//   )
//
//   useEffect(() => {
//     if (response?.type === 'success') {
//       const { code } = response.params
//       // Exchange code for tokens using casdoorDiscovery.tokenEndpoint
//     }
//   }, [response])


// ─── Option B: react-native-app-auth ─────────────────────────────────────────

// import * as AppAuth from 'react-native-app-auth'
//
// export const casdoorConfig: AppAuth.AuthConfiguration = {
//   issuer:                `${CASDOOR_URL}`,
//   clientId:              CLIENT_ID,
//   redirectUrl:           REDIRECT_URI,
//   scopes:                ['openid', 'profile', 'email'],
//   usePKCE:               true,
//   serviceConfiguration: {
//     authorizationEndpoint: `${CASDOOR_URL}/login/oauth/authorize`,
//     tokenEndpoint:         `${CASDOOR_URL}/api/login/oauth/access_token`,
//   },
// }
//
// // Sign in:
// const tokens = await AppAuth.authorize(casdoorConfig)
// await SecureStore.setItemAsync('accessToken',  tokens.accessToken)
// await SecureStore.setItemAsync('refreshToken', tokens.refreshToken)
//
// // Refresh:
// const refreshed = await AppAuth.refresh(casdoorConfig, { refreshToken })
//
// // Sign out:
// await AppAuth.revoke(casdoorConfig, { tokenToRevoke: accessToken })
