import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  serverExternalPackages: ["ws", "dompurify"],
  env: {
    NEXT_PUBLIC_OWNER_NAME: process.env.ROBOTHOR_OWNER_NAME || "there",
    NEXT_PUBLIC_AI_NAME: process.env.ROBOTHOR_AI_NAME || "Robothor",
  },
};

export default nextConfig;
