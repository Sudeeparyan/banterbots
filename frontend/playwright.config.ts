import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir:'./e2e',fullyParallel:false,workers:1,retries:0,
  timeout:45000,expect:{timeout:15000},
  reporter:'list',outputDir:'test-results',
  use:{baseURL:process.env.BANTERBOTS_TEST_URL??'http://127.0.0.1:8000',browserName:'chromium',headless:true,viewport:{width:1440,height:1100},trace:'retain-on-failure',screenshot:'only-on-failure'},
});
