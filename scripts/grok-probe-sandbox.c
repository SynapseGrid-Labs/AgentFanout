/* Process-local, credential-free diagnostic boundary. Never an admission proof.
 * Linux x86_64 only; fail closed on unsupported Landlock ABIs. No privileges,
 * namespaces, services, host settings, or external libraries are required.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/landlock.h>
#include <linux/capability.h>
#include <linux/seccomp.h>
#include <sched.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#if !defined(__x86_64__)
#error Unsupported architecture
#endif

static void fail(const char *step) {
    fprintf(stderr, "probe_boundary_unavailable:%s:%d\n", step, errno);
    exit(125);
}
static void rule(int rules, const char *path, unsigned long long rights) {
    int fd = open(path, O_PATH | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) fail("open_rule");
    struct landlock_path_beneath_attr attr = {.allowed_access=rights, .parent_fd=fd};
    if (syscall(SYS_landlock_add_rule, rules, LANDLOCK_RULE_PATH_BENEATH, &attr, 0))
        fail("add_rule");
    close(fd);
}
#define ST(code,k) ((struct sock_filter)BPF_STMT(code,k))
#define JP(code,k,t,f) ((struct sock_filter)BPF_JUMP(code,k,t,f))
static void filter(void) {
    struct sock_filter b[512];
    unsigned n=0;
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data,arch));
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,AUDIT_ARCH_X86_64,1,0);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr));
    /* x32 and every unknown syscall fail closed. clone3 must signal ENOSYS
     * so libc may fall back to the precisely checked pthread clone below. */
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,SYS_clone3,0,1);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|ENOSYS);
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,SYS_clone,0,8);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0])+4);
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,0,1,0);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0]));
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,
        CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|
        CLONE_SYSVSEM|CLONE_SETTLS|CLONE_PARENT_SETTID|CLONE_CHILD_CLEARTID,2,0);
    /* Installed static runtime also passes the ignored legacy DETACHED flag.
     * Both accepted combinations require THREAD and share the same address
     * space/files; every other flag combination and high word is denied. */
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,
        CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|
        CLONE_SYSVSEM|CLONE_SETTLS|CLONE_PARENT_SETTID|CLONE_CHILD_CLEARTID|CLONE_DETACHED,1,0);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    /* Tokio signal wakeups need an unnamed in-process Unix stream pair. No
     * socket(), bind(), connect(), listen(), accept(), named or abstract Unix
     * endpoint is permitted, and no inherited socket exists. */
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,SYS_socketpair,0,11);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0]));
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,AF_UNIX,1,0);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[1]));
    b[n++]=ST(BPF_ALU|BPF_AND|BPF_K,~(SOCK_CLOEXEC|SOCK_NONBLOCK));
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,SOCK_STREAM,1,0);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[2]));
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,0,1,0);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,SYS_ioctl,0,4);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[1]));
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,FIONBIO,1,0);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,SYS_sendto,0,8);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[4]));
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,0,0,5);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[4])+4);
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,0,0,3);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[5]));
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,0,0,1);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,SYS_tgkill,0,4);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0]));
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,(unsigned)getpid(),1,0);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,SYS_prlimit64,0,8);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0]));
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,0,0,5);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[2]));
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,0,0,3);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[2])+4);
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,0,0,1);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,SYS_openat,0,4);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[2]));
    b[n++]=JP(BPF_JMP|BPF_JSET|BPF_K,O_PATH,0,1);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    /* Static musl uses legacy open rather than glibc's openat. Same Landlock
     * mediation, with the flags argument at a different syscall index. */
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,SYS_open,0,4);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[1]));
    b[n++]=JP(BPF_JMP|BPF_JSET|BPF_K,O_PATH,0,1);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,SYS_fcntl,0,
#ifdef AGENTFANOUT_PRIVATE_CONTAINER_DIAGNOSTIC
             22
#else
             16
#endif
    );
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[1]));
    const int commands[]={F_GETFD,F_SETFD,F_GETFL,F_DUPFD,F_DUPFD_CLOEXEC
#ifdef AGENTFANOUT_PRIVATE_CONTAINER_DIAGNOSTIC
        ,F_GETLK,F_SETLK,F_SETLKW
#endif
    };
    for(unsigned i=0;i<sizeof(commands)/sizeof(commands[0]);i++) {
        b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,commands[i],0,1);
        b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    }
    b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,F_SETFL,0,3);
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[2]));
    b[n++]=JP(BPF_JMP|BPF_JSET|BPF_K,O_ASYNC,1,0);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    /* Reload after argument checks. Landlock mediates all allowed opens,
     * traversal, directory reads, creates, renames, truncates and execution.
     * Path-only metadata, links, other ioctl/sockets, io_uring, external IPC, ptrace, process
     * memory, pidfds, mount, capabilities, fork and vfork are not allowed. */
    b[n++]=ST(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr));
    const int allowed[]={
#ifdef AGENTFANOUT_PRIVATE_CONTAINER_DIAGNOSTIC
        /* Only in a separately host-verified private PID/mount/container root.
         * No host workspace/config/proc/device namespace may be mounted. */
        SYS_newfstatat,SYS_stat,SYS_lstat,SYS_statx,SYS_readlink,SYS_readlinkat,
        SYS_access,SYS_faccessat,SYS_faccessat2,SYS_flock,
#endif
        SYS_read,SYS_write,SYS_readv,SYS_writev,SYS_close,SYS_lseek,
        SYS_pread64,SYS_pwrite64,SYS_openat,SYS_fstat,SYS_getdents64,
        SYS_recvfrom,
        SYS_dup,SYS_dup2,SYS_dup3,SYS_fsync,SYS_fdatasync,
        SYS_ftruncate,SYS_mkdir,SYS_mkdirat,SYS_unlink,SYS_unlinkat,SYS_rename,SYS_renameat,SYS_renameat2,
        SYS_mmap,SYS_mprotect,SYS_munmap,SYS_mremap,SYS_madvise,SYS_brk,
        SYS_futex,SYS_set_tid_address,SYS_set_robust_list,SYS_rseq,
        SYS_rt_sigaction,SYS_rt_sigprocmask,SYS_rt_sigreturn,SYS_sigaltstack,
        SYS_getrandom,SYS_clock_gettime,SYS_clock_getres,SYS_nanosleep,
        SYS_clock_nanosleep,SYS_epoll_create1,SYS_epoll_ctl,SYS_epoll_wait,
        SYS_epoll_pwait,SYS_eventfd2,SYS_poll,SYS_ppoll,SYS_select,SYS_pselect6,
        SYS_getpid,SYS_gettid,SYS_getcwd,SYS_getuid,SYS_geteuid,SYS_getgid,SYS_getegid,
        SYS_getgroups,SYS_uname,SYS_sched_getaffinity,SYS_sched_yield,
        SYS_getrlimit,SYS_getrusage,SYS_arch_prctl,SYS_restart_syscall,
        SYS_execve,SYS_exit,SYS_exit_group
    };
    for (unsigned i=0;i<sizeof(allowed)/sizeof(allowed[0]);i++) {
        b[n++]=JP(BPF_JMP|BPF_JEQ|BPF_K,allowed[i],0,1);
        b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);
    }
    b[n++]=ST(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
    struct sock_fprog p={.len=(unsigned short)n,.filter=b};
    if (prctl(PR_SET_SECCOMP,SECCOMP_MODE_FILTER,&p)) fail("seccomp");
}
int main(int argc,char **argv) {
    if (argc<3) {errno=EINVAL;fail("arguments");}
    if (getuid()==0 || geteuid()!=getuid() || getegid()!=getgid()) {
        errno=EPERM;fail("privileged_identity");
    }
#ifdef AGENTFANOUT_PRIVATE_CONTAINER_DIAGNOSTIC
    struct __user_cap_header_struct cap_header={.version=_LINUX_CAPABILITY_VERSION_3,.pid=0};
    struct __user_cap_data_struct cap_data[2];
    if(getpid()!=1 || syscall(SYS_capget,&cap_header,&cap_data) ||
       cap_data[0].effective || cap_data[1].effective ||
       cap_data[0].permitted || cap_data[1].permitted ||
       cap_data[0].inheritable || cap_data[1].inheritable) {
        errno=EPERM;fail("private_container_identity");
    }
    /* Only disposable quota-bounded tmpfs supplied by the verified host
     * container contract. No operator files, credentials or project data. */
    if(mkdir("/work/home",0700) || mkdir("/work/home/.grok",0700) ||
       mkdir("/work/task",0700) || mkdir("/work/tmp",0700)) fail("private_home");
    int config=open("/work/home/.grok/config.toml",O_WRONLY|O_CREAT|O_EXCL,0600);
    const char profile[]="[auth]\npreferred_method = \"oidc\"\ndisable_api_key_auth = true\n";
    if(config<0 || write(config,profile,sizeof(profile)-1)!=(ssize_t)(sizeof(profile)-1)) fail("private_profile");
    close(config);
    clearenv();
    if(setenv("HOME","/work/home",1) || setenv("XDG_CONFIG_HOME","/work/home",1) ||
       setenv("PATH","",1) || setenv("LANG","C",1) || setenv("TMPDIR","/work/tmp",1) ||
       setenv("TOKIO_WORKER_THREADS","2",1) || setenv("RAYON_NUM_THREADS","2",1)) fail("private_environment");
#endif
    int abi=syscall(SYS_landlock_create_ruleset,NULL,0,LANDLOCK_CREATE_RULESET_VERSION);
    if (abi<3 || abi>4) {errno=ENOTSUP;fail("landlock_abi");}
    if (prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0)) fail("no_new_privs");
    /* ABI 3/4 cover execute through truncate. No unsupported rights silently
     * disappear. Network is denied by syscall allowlist, not TCP-only rules. */
    struct landlock_ruleset_attr attr={.handled_access_fs=(1ULL<<15)-1};
    int rules=syscall(SYS_landlock_create_ruleset,&attr,sizeof(attr),0);
    if (rules<0) fail("create_ruleset");
    rule(rules,argv[1],LANDLOCK_ACCESS_FS_EXECUTE|LANDLOCK_ACCESS_FS_READ_FILE);
    /* First diagnostic allows no filesystem mutations, even in disposable
     * state. A binary requiring writes fails safely; never widen on retry. */
    rule(rules,argv[2],LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_READ_DIR
#ifdef AGENTFANOUT_PRIVATE_CONTAINER_DIAGNOSTIC
         |LANDLOCK_ACCESS_FS_WRITE_FILE|LANDLOCK_ACCESS_FS_REMOVE_DIR|
         LANDLOCK_ACCESS_FS_REMOVE_FILE|LANDLOCK_ACCESS_FS_MAKE_DIR|
         LANDLOCK_ACCESS_FS_MAKE_REG|LANDLOCK_ACCESS_FS_REFER|LANDLOCK_ACCESS_FS_TRUNCATE
#endif
    );
#ifdef AGENTFANOUT_PRIVATE_CONTAINER_DIAGNOSTIC
    if (chdir("/work/task")) fail("cwd");
#else
    if (chdir(argv[2])) fail("cwd");
#endif
    if (syscall(SYS_landlock_restrict_self,rules,0)) fail("restrict_self");
    close(rules);
    /* No inherited files/sockets/process handles. stdio must be private pipes,
     * checked by the host and here; never a terminal/device or regular file. */
    for(int i=0;i<3;i++) {
        struct stat st;
        if(fstat(i,&st) || !S_ISFIFO(st.st_mode)) {errno=EINVAL;fail("stdio_not_pipe");}
    }
    if (syscall(SYS_close_range,3,~0U,0)) fail("close_descriptors");
    struct rlimit limit={0,0};
    if(setrlimit(RLIMIT_CORE,&limit)) fail("core_limit");
    limit=(struct rlimit){8,8}; if(setrlimit(RLIMIT_CPU,&limit)) fail("cpu_limit");
    limit=(struct rlimit){64,64}; if(setrlimit(RLIMIT_NOFILE,&limit)) fail("fd_limit");
    limit=(struct rlimit){16777216,16777216}; if(setrlimit(RLIMIT_FSIZE,&limit)) fail("file_limit");
    limit=(struct rlimit){2147483648ULL,2147483648ULL};
    if(setrlimit(RLIMIT_AS,&limit)) fail("memory_limit");
    filter();
    fprintf(stderr,"probe_boundary_enforced:landlock=%d;seccomp=allowlist-v3-static-threads"
#ifdef AGENTFANOUT_PRIVATE_CONTAINER_DIAGNOSTIC
            ";private_container_root"
#endif
            "\n",abi);
    /* argv[2] was boundary data, never passed to the model process. */
    argv[2]=argv[1];
    execve(argv[1],&argv[2],environ);
    fail("exec");
}
