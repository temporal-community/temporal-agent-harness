// Globals Temporal's workflow sandbox provides that the workflow-safe package uses. It compiles
// without the DOM's or Node's global types, so a browser or Node API fails to compile here;
// these few are declared as the sandbox has them.

declare class TextEncoder {
  encode(input?: string): Uint8Array;
}

declare class TextDecoder {
  constructor(label?: string, options?: { fatal?: boolean });
  decode(input?: Uint8Array): string;
}
