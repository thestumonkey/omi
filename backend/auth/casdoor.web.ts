/**
 * Casdoor — Auth.js provider config (web)
 * =========================================
 * Works with Auth.js v5 (next-auth, @auth/sveltekit, @auth/express, etc.)
 * Swap the adapter import for your framework:
 *
 *   Next.js:    import NextAuth from 'next-auth'
 *   SvelteKit:  import { SvelteKitAuth } from '@auth/sveltekit'
 *   Express:    import { ExpressAuth } from '@auth/express'
 *
 * Install:
 *   npm install next-auth        (Next.js)
 *   npm install @auth/sveltekit  (SvelteKit)
 *   npm install @auth/express    (Express)
 *
 * Required env vars:
 *   CASDOOR_ENDPOINT       Internal URL (server → Casdoor, e.g. http://casdoor:8000)
 *   CASDOOR_EXTERNAL_URL   Browser-facing URL (e.g. http://localhost:8082)
 *   CASDOOR_CLIENT_ID      Written by casdoor-provision
 *   CASDOOR_CLIENT_SECRET  Written by casdoor-provision
 *   CASDOOR_ORG_NAME       Your org name (e.g. myapp)
 *   CASDOOR_APP_NAME       Your app name (e.g. myapp)
 *   AUTH_SECRET            Random secret: openssl rand -hex 32
 *   AUTH_URL               Public URL of your web app (e.g. http://localhost:3000)
 *
 * Split-horizon DNS
 * -----------------
 * CASDOOR_ENDPOINT is used for server-to-server calls (token exchange, userinfo,
 * JWKS). CASDOOR_EXTERNAL_URL is used for browser redirects (authorization URL).
 * If Casdoor is on the same host as the app, these can be the same value.
 */

import NextAuth from 'next-auth' // ← swap for your framework adapter

const internal = (process.env.CASDOOR_ENDPOINT     ?? '').replace(/\/$/, '')
const external = (process.env.CASDOOR_EXTERNAL_URL ?? internal).replace(/\/$/, '')

function decodeJwt(token: string): Record<string, unknown> {
  try {
    return JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString())
  } catch {
    return {}
  }
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  trustHost: true,
  providers: [
    {
      id: 'casdoor',
      name: 'Casdoor',
      type: 'oauth',
      clientId:     process.env.CASDOOR_CLIENT_ID!,
      clientSecret: process.env.CASDOOR_CLIENT_SECRET!,
      // Must match the `iss` claim in Casdoor's JWT (always the public-facing URL)
      issuer: external,
      authorization: {
        url: `${external}/login/oauth/authorize`,
        params: { scope: 'openid profile email' },
      },
      // Server-to-server: use internal URL
      token:          `${internal}/api/login/oauth/access_token`,
      userinfo:       `${internal}/api/userinfo`,
      jwks_endpoint:  `${internal}/.well-known/jwks`,
      checks: ['pkce', 'state'],
      profile(profile) {
        return {
          id:    profile.sub,
          name:  profile.displayName ?? profile.name,
          email: profile.email,
          image: profile.avatar ?? null,
        }
      },
    },
  ],
  callbacks: {
    async jwt({ token, account }) {
      // First login — store tokens
      if (account?.access_token) {
        const payload = decodeJwt(account.access_token)
        token.accessToken  = account.access_token
        token.refreshToken = account.refresh_token
        token.expiresAt    = (payload.exp as number | undefined) ?? account.expires_at
        token.sub          = (payload.sub as string | undefined) ?? token.sub
        return token
      }

      // Token still valid (60 s buffer)
      const nowSec = Math.floor(Date.now() / 1000)
      if (token.expiresAt && nowSec < (token.expiresAt as number) - 60) return token

      // Refresh
      if (!token.refreshToken) return { ...token, error: 'RefreshTokenError' }
      try {
        const res = await fetch(`${internal}/api/login/oauth/access_token`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({
            grant_type:    'refresh_token',
            refresh_token: token.refreshToken as string,
            client_id:     process.env.CASDOOR_CLIENT_ID!,
            client_secret: process.env.CASDOOR_CLIENT_SECRET!,
          }),
        })
        const tokens = await res.json() as Record<string, unknown>
        if (!res.ok) throw new Error(String(tokens.error))
        const payload = decodeJwt(tokens.access_token as string)
        token.accessToken  = tokens.access_token
        token.refreshToken = (tokens.refresh_token as string) ?? token.refreshToken
        token.expiresAt    = (payload.exp as number | undefined) ?? (nowSec + (tokens.expires_in as number))
        token.sub          = (payload.sub as string | undefined) ?? token.sub
      } catch {
        return { ...token, error: 'RefreshTokenError' }
      }
      return token
    },
    async session({ session, token }) {
      session.accessToken = token.accessToken
      session.error       = token.error
      if (session.user && token.sub) session.user.id = token.sub
      return session
    },
  },
})
