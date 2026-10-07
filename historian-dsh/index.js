// The official CLI links this local bundle; both sibling packages ship in the same image.
// Reuse YunYing's installed, pinned native DSH dependency graph, not a second installation.
export * from '../yunying-dsh/src/historian.js';
