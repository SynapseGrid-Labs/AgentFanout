#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/io_uring.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/resource.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <asm/prctl.h>
#include <sched.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

static int thread_ok=0;
static void *thread(void *unused) { (void)unused; thread_ok=1; return NULL; }
static int static_thread(void *value) {__atomic_store_n((int*)value,1,__ATOMIC_RELEASE);syscall(SYS_exit,0);return 0;}
static void check(const char *name,int success) {printf("\"%s\":%s,",name,success?"true":"false");}
int main(int argc,char **argv) {
    if(argc!=4) return 2;
    if(!strcmp(argv[1],"--no-auto-update")) {
#ifdef AGENTFANOUT_CONTAINER_FIXTURE
        argv[1]="/etc/passwd";argv[2]="50";argv[3]="checks";
#else
        printf("{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"synthetic\":true,\"home\":\"%s\"}}\n",getenv("HOME"));
        return 0;
#endif
    }
    if(!strcmp(argv[3],"timeout")) {for(;;) sleep(1);}
    if(!strcmp(argv[3],"flood")) {for(;;) if(write(1,"xxxxxxxxxxxxxxxx",16)<0)return 3;}
    if(!strcmp(argv[3],"malformed")) {puts("not-json");return 0;}
    if(!strcmp(argv[3],"callback")) {puts("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"terminal/create\"}");return 0;}
    if(!strcmp(argv[3],"wrong-id")) {puts("{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{}}");return 0;}
    if(!strcmp(argv[3],"duplicate")) {puts("{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}\n{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}");return 0;}
    if(!strcmp(argv[3],"duplicate-key")) {puts("{\"jsonrpc\":\"2.0\",\"id\":1,\"id\":2,\"result\":{}}");return 0;}
    if(!strcmp(argv[3],"crash")) {return 9;}
    printf("{");
    check("read_host_denied",open(argv[1],O_RDONLY)<0);
    check("legacy_open_host_denied",syscall(SYS_open,argv[1],O_RDONLY,0)<0);
    check("legacy_opath_denied",syscall(SYS_open,argv[1],O_PATH,0)==-1&&errno==EPERM);
    check("legacy_mkdir_host_denied",syscall(SYS_mkdir,"/agentfanout-forbidden-dir",0700)<0);
    check("write_host_denied",open(argv[1],O_WRONLY)<0);
#ifdef AGENTFANOUT_CONTAINER_FIXTURE
    int writable=open("new-file",O_CREAT|O_WRONLY,0600);
    check("write_private_tmpfs_allowed",writable>=0);if(writable>=0)close(writable);
#else
    check("write_workspace_denied",open("new-file",O_CREAT|O_WRONLY,0600)<0);
#endif
    check("truncate_host_denied",truncate(argv[1],0)<0);
    check("chmod_host_denied",chmod(argv[1],0600)<0);
#ifdef AGENTFANOUT_CONTAINER_FIXTURE
    check("stat_private_image_allowed",({struct stat st;stat(argv[1],&st)==0;}));
#else
    check("stat_host_denied",({struct stat st;stat(argv[1],&st)<0;}));
#endif
    check("opath_host_denied",open(argv[1],O_PATH)<0);
    check("symlink_escape_denied",open("escape",O_RDONLY)<0);
    check("traversal_denied",open("../../outside-secret",O_RDONLY)<0);
    check("proc_denied",open("/proc/self/environ",O_RDONLY)<0);
    check("device_denied",open("/dev/null",O_RDWR)<0);
    check("tcp_denied",socket(AF_INET,SOCK_STREAM,0)<0);
    check("udp_denied",socket(AF_INET,SOCK_DGRAM,0)<0);
    check("unix_denied",socket(AF_UNIX,SOCK_STREAM,0)<0);
    check("private_pair_allowed",({int s[2];int ok=socketpair(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0,s)==0;
        if(ok){char byte=0;int nonblock=1;ok=ioctl(s[0],FIONBIO,&nonblock)==0&&send(s[0],"x",1,0)==1&&recv(s[1],&byte,1,0)==1&&byte=='x';close(s[0]);close(s[1]);}ok;}));
    check("pair_other_family_denied",({int s[2];socketpair(AF_INET,SOCK_STREAM,0,s)<0;}));
    check("pair_datagram_denied",({int s[2];socketpair(AF_UNIX,SOCK_DGRAM,0,s)<0;}));
    check("pair_protocol_denied",({int s[2];socketpair(AF_UNIX,SOCK_STREAM,1,s)<0;}));
    check("bind_denied",syscall(SYS_bind,-1,NULL,0)==-1&&errno==EPERM);
    check("listen_denied",syscall(SYS_listen,-1,1)==-1&&errno==EPERM);
    check("named_send_denied",syscall(SYS_sendto,-1,NULL,0,0,"forbidden",1)==-1&&errno==EPERM);
    check("sendmsg_denied",syscall(SYS_sendmsg,-1,NULL,0)==-1&&errno==EPERM);
    check("connect_denied",syscall(SYS_connect,-1,NULL,0)==-1&&errno==EPERM);
    check("io_uring_denied",syscall(SYS_io_uring_setup,1,NULL)==-1&&errno==EPERM);
    check("unknown_syscall_denied",syscall(9999)==-1&&errno==EPERM);
    check("x32_denied",syscall(0x40000000)==-1&&errno==EPERM);
    check("ioctl_denied",syscall(SYS_ioctl,-1,0,0)==-1&&errno==EPERM);
    check("process_vm_denied",syscall(SYS_process_vm_readv,0,0,0,0,0,0)==-1&&errno==EPERM);
    check("fork_denied",fork()<0);
    check("vfork_denied",syscall(SYS_vfork)==-1&&errno==EPERM);
    check("clone_process_denied",syscall(SYS_clone,CLONE_VM|CLONE_VFORK,NULL,NULL,NULL,NULL)==-1&&errno==EPERM);
    check("clone_high_flags_denied",syscall(SYS_clone,1ULL<<32,NULL,NULL,NULL,NULL)==-1&&errno==EPERM);
    check("signal_other_denied",kill(getppid(),0)<0);
    check("ptrace_denied",syscall(SYS_ptrace,0,0,0,0)==-1&&errno==EPERM);
    check("exec_other_denied",({char *v[]={"/bin/true",NULL};execve(v[0],v,environ)<0;}));
    check("exec_memfd_denied",syscall(SYS_memfd_create,"tool",0)==-1&&errno==EPERM);
    check("fd_closed",fcntl(atoi(argv[2]),F_GETFD)==-1&&errno==EBADF);
    check("fcntl_signal_denied",fcntl(1,F_SETOWN,123)==-1&&errno==EPERM);
    check("async_signal_denied",fcntl(1,F_SETFL,O_ASYNC)==-1&&errno==EPERM);
    check("limit_raise_denied",({struct rlimit l={100,100};prlimit(0,RLIMIT_NOFILE,&l,NULL)<0;}));
    check("ambient_key_absent",getenv("XAI_API_KEY")==NULL);
    pthread_t t;
    int rc=pthread_create(&t,NULL,thread,NULL);
    if(!rc) pthread_join(t,NULL);
    check("thread_allowed",!rc&&thread_ok);
    unsigned long tls=0;int done=0,tid=0,child_tid=1;
    void *stack=mmap(NULL,65536,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    syscall(SYS_arch_prctl,ARCH_GET_FS,&tls);
    int flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM|CLONE_SETTLS|CLONE_PARENT_SETTID|CLONE_CHILD_CLEARTID|CLONE_DETACHED;
    rc=stack==MAP_FAILED?-1:clone(static_thread,(char*)stack+65536,flags,&done,&tid,(void*)tls,&child_tid);
    if(rc>=0) while(__atomic_load_n(&child_tid,__ATOMIC_ACQUIRE)) sched_yield();
    check("static_thread_flags_allowed",rc>=0&&__atomic_load_n(&done,__ATOMIC_ACQUIRE));
    if(stack!=MAP_FAILED)munmap(stack,65536);
    check("profile_read_allowed",({int fd=open(
#ifdef AGENTFANOUT_CONTAINER_FIXTURE
        "../home/.grok/config.toml",
#else
        "home/.grok/config.toml",
#endif
        O_RDONLY);int ok=fd>=0;if(ok)close(fd);ok;}));
    check("legacy_profile_read_allowed",({int fd=syscall(SYS_open,
#ifdef AGENTFANOUT_CONTAINER_FIXTURE
        "../home/.grok/config.toml",
#else
        "home/.grok/config.toml",
#endif
        O_RDONLY,0);int ok=fd>=0;if(ok)close(fd);ok;}));
    printf("\"done\":true}\n");
    return 0;
}
