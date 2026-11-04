export default function nextConfig(phase) {
  return {
    // Keep production builds from overwriting a running dev server's CSS/chunks.
    distDir: phase === 'phase-development-server' ? '.next-dev' : '.next',
  };
}
