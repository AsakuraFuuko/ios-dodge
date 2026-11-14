#!/usr/bin/env python3
"""Exercise the actual reset dispatch and active hardware-lease guards."""
from pathlib import Path
import subprocess,tempfile
ROOT=Path(__file__).resolve().parents[1]
s=(ROOT/'source/touch-runtime-v15/loader.c').read_text()
# The dispatch handler keeps growing; the reset/lease-guard block under test is
# the cold-reset branch plus the touch-lease guards, which end where the Wi-Fi
# handoff guards begin. Anchor on those markers instead of a line offset.
start=s.index('static void\nfb_command(char *cmd)')
end=s.index(' if(op13_wifi_votes_stuck&&',start)
prefix=s[start:end]
source=r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <setjmp.h>
#include <stdio.h>
#define READ_SYSREG(x) 1
static int platform_dodge,other;static int *PLAT;
static void *running_live;static uint64_t usb_last_activity;static int screen_quiet;
static int touch_runtime_pending,touch_runtime_outstanding;
static struct {int blocked,panel_owned;void *handle;} identify_session;
static struct {int owned;} bb_state;
static int retained,reply,stalled,resets;
static jmp_buf done;
static int touch_clock_retained(void){return retained;}
static int str_eq(const char*a,const char*b){return !strcmp(a,b);}
static int starts_with(const char*a,const char*b){return !strncmp(a,b,strlen(b));}
static void print(const char *format,...){(void)format;}
static void fb_reply(const char *a,const char*b){(void)b;reply=!strcmp(a,"OKAY")?1:2;}
static void stall(unsigned us){assert(reply==1);assert(us==200000);stalled=1;}
static void reset(int kind,int status,int bytes,void *data){assert(stalled);assert(!kind&&!status&&!bytes&&!data);resets++;longjmp(done,1);}
static struct {void(*stall)(unsigned);} boot={stall},*BS=&boot;
static struct {void(*reset_system)(int,int,int,void*);} runtime={reset};
static struct {typeof(runtime)*rt;} system_table={&runtime},*ST=&system_table;
/* The dispatch body also consults the Project Boot menu before the reset/guard
 * logic. Those menu globals live outside the extracted slice; supply the same
 * declarations and an idle menu so the exercised paths see no pending action. */
#include "op13_boot_menu.h"
static struct op13_menu boot_menu;
static unsigned menu_ios_prepared;
static int boot_menu_stop(void) { return 0; }
'''+prefix+r'''
}
int main(void){
 const char *commands[]={"reboot","oem reboot","reboot-bootloader","oem reboot-extra","getvar:result","download:1"};
 unsigned cases=0;
 for(unsigned platform=0;platform<2;platform++)for(unsigned state=0;state<16;state++)for(unsigned c=0;c<6;c++){
  PLAT=platform?&platform_dodge:&other;running_live=(state&1)?(void*)1:0;
  touch_runtime_pending=(state>>1)&1;touch_runtime_outstanding=touch_runtime_pending;
  retained=(state>>2)&1;identify_session.blocked=(state>>3)&1;
  identify_session.handle=0;identify_session.panel_owned=0;bb_state.owned=0;
  reply=stalled=resets=0;
  if(!setjmp(done))fb_command((char*)commands[c]);
  assert(resets==(platform&&c<2));
  if(platform&&c<2)assert(reply==1&&stalled);
  if(touch_runtime_pending&&c>=2&&c!=4)assert(reply==2);
  cases++;
 }
 printf("%u actual reset/guard cases passed\n",cases);
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d);(p/'test.c').write_text(source)
 subprocess.run(['/usr/bin/clang','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-variable',
                 '-Wno-unused-function','-Wno-unused-parameter','-fsanitize=address,undefined',
                 '-I',str(ROOT/'source/touch-runtime-v15'),str(p/'test.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)
