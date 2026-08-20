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
    // Test files mock next/image with a plain <img>; the LCP/bandwidth
    // concern behind no-img-element does not apply outside the app bundle.
    files: ["**/__tests__/**", "**/*.test.tsx", "**/*.test.ts"],
    rules: {
      "@next/next/no-img-element": "off",
    },
  },
]);

export default eslintConfig;
