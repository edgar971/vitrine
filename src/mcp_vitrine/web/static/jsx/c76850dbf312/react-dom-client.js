import { ReactDOMClient } from "./runtime.js";
const m = ReactDOMClient;
export default m.default ?? m;
export const { createRoot, hydrateRoot, version } = m;
