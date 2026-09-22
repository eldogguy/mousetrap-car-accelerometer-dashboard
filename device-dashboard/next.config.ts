import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Lets classroom devices on the LAN load the dev server's JS/HMR
  // resources when hitting it by IP instead of localhost.
  allowedDevOrigins: ["192.168.4.22"],
};

export default nextConfig;
