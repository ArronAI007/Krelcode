import {build} from "esbuild";
import {fileURLToPath} from "node:url";

const shared = fileURLToPath(new URL("../shared/src/index.ts", import.meta.url));
const sdk = fileURLToPath(new URL("../../typescript-sdk/src/index.ts", import.meta.url));

await build({
  entryPoints: ["src/extension.ts"],
  outfile: "dist/extension.js",
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node20",
  sourcemap: true,
  external: ["vscode"],
  alias: {
    "@krelcode/editor-bridge": shared,
    "@krelcode/sdk": sdk,
  },
  logLevel: "info",
});
