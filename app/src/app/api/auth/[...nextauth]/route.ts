// Auth.js (next-auth v5) route handler — mounts /api/auth/* (signin, callback,
// signout, session). All SSO/OIDC handshake + bridge-token exchange happens via
// the config in @/lib/auth.
import { handlers } from "@/lib/auth";

export const { GET, POST } = handlers;
