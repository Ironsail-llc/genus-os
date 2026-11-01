import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
  // eslint-plugin-react-hooks 7.1 (with eslint-config-next 16.4) is stricter
  // about its React Compiler rules than 7.0 was: 51 findings across 18 files
  // that passed before. Each rule is off only in the files that break it, so
  // the rest of src/ stays guarded and a file leaves its list once reworked.
  {
    files: [
      "src/components/chat-panel.tsx",
      "src/components/personal-automation-panel.tsx",
    ],
    rules: { "react-hooks/immutability": "off" },
  },
  {
    files: [
      "src/components/chat-panel.tsx",
    ],
    rules: { "react-hooks/preserve-manual-memoization": "off" },
  },
  {
    files: [
      "src/components/chat-ask-card.tsx",
      "src/hooks/use-agents.ts",
      "src/hooks/use-tasks.ts",
    ],
    rules: { "react-hooks/purity": "off" },
  },
  {
    files: [
      "src/components/chat-panel.tsx",
      "src/lib/bridge/use-bridge-poll.ts",
    ],
    rules: { "react-hooks/refs": "off" },
  },
  {
    files: [
      "src/app/setup/setup-wizard.tsx",
      "src/components/chat-panel.tsx",
      "src/components/personal-automation-panel.tsx",
      "src/components/views/agents/agent-manifests.tsx",
      "src/components/views/agents/agent-panel.tsx",
      "src/components/views/audit-view.tsx",
      "src/components/views/canvas-view.tsx",
      "src/components/views/marketplace-view.tsx",
      "src/components/views/memory-view.tsx",
      "src/components/views/runs-view.tsx",
      "src/components/views/settings/config-page.tsx",
      "src/components/views/settings/flags-page.tsx",
      "src/components/views/settings/providers-page.tsx",
      "src/components/views/settings/users-page.tsx",
      "src/hooks/use-agents.ts",
      "src/hooks/use-tasks.ts",
    ],
    rules: { "react-hooks/set-state-in-effect": "off" },
  },
  {
    files: [
      "src/components/views/agents/agent-panel.tsx",
    ],
    rules: { "react-hooks/static-components": "off" },
  },
  {
    // Debug prints do not ship: a dashboard log line is a warning or an
    // error, or it goes to /api/dashboard/error.
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "no-console": ["error", { allow: ["warn", "error"] }],
    },
  },
  {
    // Test files mock next/image with a plain <img>; the LCP/bandwidth
    // concern behind no-img-element does not apply outside the app bundle.
    files: ["**/__tests__/**", "**/*.test.tsx", "**/*.test.ts"],
    rules: {
      "@next/next/no-img-element": "off",
    },
  },
]);

export default eslintConfig;
