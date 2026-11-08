/**
 * Package entry for `dsh-plugin-keyless-web`.
 *
 * The implementation lives in {@link ./keyless-web-provider.mjs} so the bundle
 * patch, the self-check and a direct `name: <path>.mjs` overlay all load the
 * same module; this file only re-exports it as the package root, which is what
 * a profile's `dsh.profile.bundles` entry resolves.
 */

export { apply, inject, name, normalizeConfig, resolvePython } from './keyless-web-provider.mjs'
