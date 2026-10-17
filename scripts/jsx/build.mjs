// Bundles the pinned packages into the browser ES modules the JSX renderer serves.
// Run through `just vendor-jsx` (scripts/vendor_jsx.py), never by hand: that script installs the
// lockfile, records the hashes and licences, and places the output in the package.
//
// usage: node build.mjs <out-dir> <metafile>
import { build } from "esbuild";
import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

const require = createRequire(import.meta.url);
const [out, metafile] = process.argv.slice(2);
mkdirSync(out, { recursive: true });

// One bundle holds the one copy of React; each specifier the import map names re-exports from it.
const SHARED = {
  react: ["React", "react.js"],
  "react-dom": ["ReactDOM", "react-dom.js"],
  "react-dom/client": ["ReactDOMClient", "react-dom-client.js"],
  "react/jsx-runtime": ["JSXRuntime", "jsx-runtime.js"],
};
// Packages agents import most, bundled against the shared React rather than their own copy.
const PACKAGES = { recharts: "recharts.js", "lucide-react": "lucide-react.js" };

const common = {
  bundle: true,
  format: "esm",
  minify: true,
  legalComments: "inline",
  target: "es2022",
  platform: "browser",
  define: { "process.env.NODE_ENV": '"production"' },
  logLevel: "warning",
  metafile: true,
};
const inputs = {};
const record = (result) => Object.assign(inputs, result.metafile.inputs);

const names = (specifier) =>
  Object.keys(require(specifier))
    .filter((key) => key !== "default" && /^[A-Za-z_$][\w$]*$/.test(key))
    .sort();

const entries = Object.entries(SHARED);
const runtime = [
  ...entries.map(([specifier, [name]]) => `import * as ${name} from ${JSON.stringify(specifier)};`),
  `export { ${entries.map(([, [name]]) => name).join(", ")} };`,
  `globalThis.__vitrine_react__ = { ${entries
    .map(([specifier, [name]]) => `${JSON.stringify(specifier)}: ${name}`)
    .join(", ")} };`,
].join("\n");
record(
  await build({
    ...common,
    stdin: { contents: runtime, resolveDir: process.cwd(), loader: "js" },
    outfile: join(out, "runtime.js"),
  }),
);
for (const [specifier, [name, file]] of entries) {
  writeFileSync(
    join(out, file),
    [
      `import { ${name} } from "./runtime.js";`,
      `const m = ${name};`,
      "export default m.default ?? m;",
      `export const { ${names(specifier).join(", ")} } = m;`,
      "",
    ].join("\n"),
  );
}

const shared = {
  name: "shared-react",
  setup(builder) {
    builder.onResolve({ filter: /^react(-dom)?(\/client|\/jsx-runtime)?$/ }, (args) => ({
      path: args.path,
      namespace: "shared",
    }));
    builder.onLoad({ filter: /.*/, namespace: "shared" }, (args) => ({
      contents: `const m = globalThis.__vitrine_react__[${JSON.stringify(args.path)}]; module.exports = m.default ?? m;`,
      loader: "js",
    }));
  },
};
for (const [specifier, file] of Object.entries(PACKAGES)) {
  record(
    await build({
      ...common,
      plugins: [shared],
      banner: { js: 'import "./runtime.js";' },
      stdin: { contents: `export * from ${JSON.stringify(specifier)};`, resolveDir: process.cwd(), loader: "js" },
      outfile: join(out, file),
    }),
  );
}

record(
  await build({
    ...common,
    stdin: { contents: 'export { transform } from "sucrase";', resolveDir: process.cwd(), loader: "js" },
    outfile: join(out, "transpiler.js"),
  }),
);

copyFileSync(join("node_modules", "@tailwindcss", "browser", "dist", "index.global.js"), join(out, "tailwind.js"));
inputs["node_modules/@tailwindcss/browser/dist/index.global.js"] = {};

writeFileSync(metafile, JSON.stringify(Object.keys(inputs).sort(), null, 2));
