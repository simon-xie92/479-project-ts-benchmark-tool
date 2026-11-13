// One uniform ESLint configuration applied to EVERY repository, so lint counts are
// comparable. The repo's own ESLint config (if any) is deliberately ignored.
//
//  * eslint:recommended + typescript-eslint:recommended  -> lint errors / bad practices
//  * eslint-plugin-sonarjs:recommended                   -> code smells (SonarQube-style rules)
//  * `complexity` with max 0                             -> NOT a quality rule: it makes ESLint
//    report the cyclomatic complexity of every function, which the pipeline parses and then
//    removes from the violation counts (see bench/parsers.py::parse_eslint_json).
//
// Type-aware rules are intentionally not enabled: they need a working tsconfig/program for
// each repo, which would make failures depend on the repo's build setup.
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import sonarjs from "eslint-plugin-sonarjs";

export default [
  {
    ignores: [
      "**/node_modules/**", "**/dist/**", "**/build/**", "**/coverage/**", "**/out/**",
      "**/.next/**", "**/.stryker-tmp/**", "**/reports/**", "**/*.d.ts",
      // TypeScript only: ESLint lints *.js/*.mjs/*.cjs by default, which would pull in config files.
      "**/*.js", "**/*.jsx", "**/*.mjs", "**/*.cjs",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  sonarjs.configs.recommended,
  {
    files: ["**/*.ts", "**/*.tsx", "**/*.mts", "**/*.cts"],
    linterOptions: { reportUnusedDisableDirectives: "off" },
    rules: {
      complexity: ["warn", { max: 0 }],
    },
  },
];
