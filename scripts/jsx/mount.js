// Mounts the default export of the source vitrine inlined in the page, with nothing fetched
// from anywhere but vitrine. The page runs in an opaque origin (the sandbox CSP), so the source
// is inlined rather than fetched, and every failure is shown as text, never as a blank page.
import { transform } from "./transpiler.js";
import { Component, createElement } from "react";
import { createRoot } from "react-dom/client";

const page = document.getElementById("vitrine-source");
const root = document.getElementById("root");
const vendored = new Set(JSON.parse(page.dataset.packages));
const cdn = page.dataset.cdn || "";
// `from "x"`, `import "x"` and `import("x")`: the specifiers a component's imports name.
const SPECIFIER = /(\bfrom\s*|\bimport\s*\(?\s*)(["'])([^"'\n]+)\2/g;

function show(title, detail) {
  const box = document.createElement("pre");
  box.className = "vitrine-message";
  box.textContent = detail ? `${title}\n\n${detail}` : title;
  root.replaceChildren(box);
}

function resolve(code) {
  let problem = null;
  const resolved = code.replace(SPECIFIER, (match, lead, quote, specifier) => {
    if (vendored.has(specifier) || /^[a-z][a-z0-9+.-]*:/i.test(specifier)) {
      return match;
    }
    if (specifier.startsWith(".") || specifier.startsWith("/")) {
      problem ??= `this component imports ${specifier}: publish a multi-file app as a built site`;
      return match;
    }
    if (cdn) {
      return `${lead}${quote}${cdn}${specifier}?external=react,react-dom${quote}`;
    }
    problem ??= `package not available offline: ${specifier}`;
    return match;
  });
  return { resolved, problem };
}

class Boundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  render() {
    if (this.state.error === null) {
      return this.props.children;
    }
    const error = this.state.error;
    return createElement(
      "pre",
      { className: "vitrine-message" },
      `The component failed while rendering.\n\n${error?.stack ?? error}`,
    );
  }
}

async function main() {
  const source = JSON.parse(page.textContent);
  const transforms = page.dataset.kind === "tsx" ? ["typescript", "jsx"] : ["jsx"];
  let code;
  try {
    code = transform(source, { transforms, jsxRuntime: "automatic", production: true }).code;
  } catch (error) {
    show("This component does not compile.", String(error?.message ?? error));
    return;
  }
  const { resolved, problem } = resolve(code);
  if (problem !== null) {
    show(problem);
    return;
  }
  let module;
  try {
    module = await import(URL.createObjectURL(new Blob([resolved], { type: "text/javascript" })));
  } catch (error) {
    show("This component failed to load.", String(error?.stack ?? error));
    return;
  }
  if (typeof module.default !== "function" && typeof module.default?.$$typeof !== "symbol") {
    show("This file has no default export to render: export default function App() { … }");
    return;
  }
  createRoot(root).render(createElement(Boundary, null, createElement(module.default)));
}

main();
