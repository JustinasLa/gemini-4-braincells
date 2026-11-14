import { cpSync, mkdirSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";

const destination = new URL("../../public/trackdown/", import.meta.url);
rmSync(destination, { recursive: true, force: true });
mkdirSync(destination, { recursive: true });
cpSync(new URL("../dist/", import.meta.url), destination, { recursive: true });
console.log(`Shipped Trackdown to ${fileURLToPath(destination)}`);
