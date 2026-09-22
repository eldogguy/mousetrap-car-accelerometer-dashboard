import "dotenv/config";
import { PrismaLibSql } from "@prisma/adapter-libsql";
import { PrismaClient } from "@/app/generated/prisma/client";

const globalForPrisma = globalThis as unknown as { prisma?: PrismaClient };

// Same libSQL client works locally (DATABASE_URL="file:./dev.db", no auth
// token needed) and against a hosted Turso database in production
// (DATABASE_URL="libsql://...", TURSO_AUTH_TOKEN set) -- no code branching
// between environments, just different env vars.
const adapter = new PrismaLibSql({
  url: process.env.DATABASE_URL!,
  authToken: process.env.TURSO_AUTH_TOKEN,
});

export const prisma = globalForPrisma.prisma ?? new PrismaClient({ adapter });

if (process.env.NODE_ENV !== "production") {
  globalForPrisma.prisma = prisma;
}
