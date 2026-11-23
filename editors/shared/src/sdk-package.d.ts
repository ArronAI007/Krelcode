/**
 * Build-time shim for the local SDK package.
 *
 * The SDK's published declarations are generated under `typescript-sdk/dist`.
 * During a source checkout that directory may not exist yet; the adapter only
 * needs the dynamically imported surface consumed below.
 */
declare module "@krelcode/sdk";
