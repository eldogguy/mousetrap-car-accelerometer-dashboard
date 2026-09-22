-- CreateTable
CREATE TABLE "Device" (
    "key" TEXT NOT NULL PRIMARY KEY,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "lastSeenAt" DATETIME NOT NULL
);

-- CreateTable
CREATE TABLE "Run" (
    "id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    "deviceKey" TEXT NOT NULL,
    "csvRaw" TEXT NOT NULL,
    "sampleCount" INTEGER NOT NULL,
    "uploadedAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "Run_deviceKey_fkey" FOREIGN KEY ("deviceKey") REFERENCES "Device" ("key") ON DELETE CASCADE ON UPDATE CASCADE
);

-- CreateIndex
CREATE INDEX "Run_deviceKey_uploadedAt_idx" ON "Run"("deviceKey", "uploadedAt");
