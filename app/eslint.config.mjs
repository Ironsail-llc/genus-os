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
  {
    // eslint-config-next 16.4 turns on the React Compiler rules of
    // eslint-plugin-react-hooks 7. Code written before them breaks 51 times
    // across 18 files; they stay off here until that code is reworked, so the
    // lint pin can match the Next the app runs without one change rewriting
    // half the dashboard. New code should already pass them.
    rules: {
      "react-hooks/set-state-in-effect": "off",
      "react-hooks/static-components": "off",
      "react-hooks/preserve-manual-memoization": "off",
      "react-hooks/purity": "off",
      "react-hooks/refs": "off",
      "react-hooks/immutability": "off",
    },
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
