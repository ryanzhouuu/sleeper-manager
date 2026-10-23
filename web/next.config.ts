import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async headers() {
    return ["/manage/:path*", "/ack/:path*"].map((source) => ({
      source,
      headers: [
        { key: "Cache-Control", value: "private, no-store, max-age=0" },
        { key: "Referrer-Policy", value: "same-origin" },
        { key: "X-Content-Type-Options", value: "nosniff" },
      ],
    }));
  },
  images: {
    remotePatterns: [
      { protocol: "https", hostname: "a.espncdn.com" },
      { protocol: "https", hostname: "sleepercdn.com" },
    ],
  },
};

export default nextConfig;
