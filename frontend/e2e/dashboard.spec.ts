import {expect,test} from '@playwright/test';
import type {Page} from '@playwright/test';

async function enterStudio(page:Page){
  const errors:string[]=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error')errors.push(message.text());});
  await page.goto('/');
  await expect(page.getByRole('heading',{name:'The rivalry is live.'})).toBeVisible();
  await expect(page.getByText('Studio connected',{exact:true})).toBeAttached();
  await expect(page.getByRole('button',{name:'Advance one play'})).toBeEnabled();
  return errors;
}
async function noOverflow(page:Page){
  const dimensions=await page.evaluate(()=>({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth}));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
}

test('desktop: real A2A replay, alternating leads, debugger and reset',async({page},testInfo)=>{
  const errors=await enterStudio(page);
  await expect(page.getByRole('button',{name:/^Filter /})).toHaveCount(32);
  await expect.poll(()=>page.locator('.team-option img').evaluateAll(images=>images.every(img=>(img as HTMLImageElement).complete&&(img as HTMLImageElement).naturalWidth>0))).toBe(true);
  await expect(page.getByRole('option',{name:/OpenAI · add credentials/})).toBeDisabled();
  await page.getByRole('button',{name:'Advance one play'}).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
  const firstPair=await page.locator('.turn-meta strong').allTextContents();
  expect(firstPair).toEqual(['Max Carter','Riley Brooks']);
  const hashes=await page.locator('.turn-proof').allTextContents();
  expect(hashes.map(value=>value.match(/snapshot (\w+)/)?.[1])).toEqual([expect.any(String),hashes[0].match(/snapshot (\w+)/)?.[1]]);
  await expect(page.getByRole('button',{name:'Advance one play'})).toBeEnabled();
  await page.getByRole('button',{name:'Advance one play'}).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(4);
  expect((await page.locator('.turn-meta strong').allTextContents()).slice(2)).toEqual(['Riley Brooks','Max Carter']);
  await page.getByRole('button',{name:/Agent debugger/}).click();
  await expect(page.locator('.trace-list')).toContainText('a2a');
  await page.locator('.trace-list button').filter({hasText:'a2a'}).first().click();
  await expect(page.locator('.trace-grid pre')).toContainText('snapshot_hash');
  await noOverflow(page);
  await page.screenshot({path:testInfo.outputPath('desktop-studio.png'),fullPage:true});
  expect(errors).toEqual([]);
  await page.getByRole('button',{name:'Restart game'}).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(0);
  await expect(page.getByText('Great rivals make great radio.')).toBeVisible();
});

test('mobile: broadcast controls, complete team rail and readable layout',async({page},testInfo)=>{
  await page.setViewportSize({width:390,height:844});
  const errors=await enterStudio(page);
  await expect(page.getByRole('button',{name:/^Filter /})).toHaveCount(32);
  await page.getByRole('button',{name:'Advance one play'}).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
  await expect(page.getByRole('button',{name:'Start broadcast'})).toBeVisible();
  await noOverflow(page);
  await page.screenshot({path:testInfo.outputPath('mobile-studio.png'),fullPage:true});
  expect(errors).toEqual([]);
});

test('live feed failures remain explicit and do not fall back to replay',async({page})=>{
  const errors=await enterStudio(page);
  await page.route('**/api/games?mode=live*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({games:[],feed:{status:'error',last_success:null,error:'ESPN fixture outage'}})}));
  await page.getByRole('button',{name:'Live feed',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('ESPN fixture outage');
  await expect(page.getByRole('button',{name:'Start broadcast'})).toBeDisabled();
  await expect(page.locator('.score-list')).toContainText('No live games');
  await noOverflow(page);
  expect(errors).toEqual([]);
});
