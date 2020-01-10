#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <stdio.h>

#define MSR_IA32_FEATURE_CONTROL                0x3a
#define MSR_IA32_FEATURE_CONTROL_LOCKED         0x1
#define MSR_IA32_FEATURE_CONTROL_VMXON_ENABLED  0x4

#define SVM_CPUID_FEATURE_SHIFT 2
#define SVM_CPUID_FUNC 0x8000000a
#define SVM_VM_CR_SVM_DISABLE 4
#define MSR_VM_CR       0xc0010114

static __inline__ void cpuid(int op, unsigned int *eax, unsigned int *ebx,
			 unsigned int *ecx, unsigned int *edx)
{
	__asm__("cpuid"
		: "=a" (*eax),
		  "=b" (*ebx),
		  "=c" (*ecx),
		  "=d" (*edx)
		: "0" (op));
}
static __inline__ unsigned int cpuid_ecx(unsigned int op)
{
	unsigned int eax, ecx;

	__asm__("cpuid"
		: "=a" (eax), "=c" (ecx)
		: "0" (op)
		: "bx", "dx" );
	return ecx;
}

unsigned long cpuid_amd[3] = {0x68747541 /* "Auth" */, 0x69746e65 /* "enti" */, 0x444d4163 /* "cAMD" */};
unsigned long cpuid_intel[3] = {0x756e6547 /* "Genu" */, 0x49656e69 /* "ineI" */, 0x6c65746e /* "ntel" */};

static int test_bit(int nr, const volatile void * addr)
{
	return ((1UL << (nr & 31)) & (((const volatile unsigned int *) addr)[nr >> 5])) != 0;
}

static int cpu_has_vmx_support(void)
{
	unsigned long ecx = cpuid_ecx(1);
	return test_bit(5, &ecx); /* CPUID.1:ECX.VMX[bit 5] -> VT */
}

static int prdmsr(int cpu, unsigned long index, unsigned long *val) {
	char cpuname[16];
	int fh, ret;

	snprintf (cpuname,15, "/dev/cpu/%d/msr", cpu);
	fh = open (cpuname, O_RDWR);

	if (fh==-1)
		ret = -1;
	else {
		lseek (fh, index, SEEK_CUR);
		ret = (read (fh, val, 8) == 8);
		close (fh);
	}

	return (ret);
}

static int vmx_disabled_by_bios(void)
{
	unsigned long msr;

	prdmsr(0, MSR_IA32_FEATURE_CONTROL, &msr);
	return (msr & (MSR_IA32_FEATURE_CONTROL_LOCKED |
		       MSR_IA32_FEATURE_CONTROL_VMXON_ENABLED))
	    != MSR_IA32_FEATURE_CONTROL_LOCKED;
	/* locked but not enabled */
}

static int cpu_has_svm_support(void)
{
	unsigned int eax, ebx, ecx, edx;

	cpuid(0x80000000, &eax, &ebx, &ecx, &edx);
	if (eax < SVM_CPUID_FUNC) {
		return 0;
	}

	cpuid(0x80000001, &eax, &ebx, &ecx, &edx);
	if (!(ecx & (1 << SVM_CPUID_FEATURE_SHIFT))) {
		return 0;
	}
	return 1;
}

static int svm_disabled_by_bios(void)
{
	unsigned long vm_cr;

	prdmsr(0, MSR_VM_CR, &vm_cr);
	if (vm_cr & (1 << SVM_VM_CR_SVM_DISABLE))
		return 0;

	return 1;
}

#define INTEL 1
#define AMD   2

static int intel_or_amd(void)
{
	unsigned int eax, ebx, ecx, edx;
 
        cpuid(0, &eax, &ebx, &ecx, &edx);
	if (ebx == cpuid_intel[0] && edx == cpuid_intel[1] && ecx == cpuid_intel[2])
		return INTEL;
	else if (ebx == cpuid_amd[0] && edx == cpuid_amd[1] && ecx == cpuid_amd[2])
 		return AMD;

	return 0;	 
}

#define DONT_KNOW_CPU_ERR -128

int main(int argc, char *argv[]) {

	int bios_en = 0;
	int has_cpu = 0;
	int is_intel_or_amd;
	int ret = 0;

	printf("Welcome to the VT/SVM capability test\n");

	is_intel_or_amd = intel_or_amd();
	if (is_intel_or_amd == 0) {
		printf("Cannot know cpu type\n");
		return DONT_KNOW_CPU_ERR;
	}
	ret |= is_intel_or_amd;

	switch (is_intel_or_amd) {
	case INTEL:
		bios_en = vmx_disabled_by_bios(); 
		has_cpu = cpu_has_vmx_support();
		printf("You have Intel cpu\n");
		break;
	case AMD:
		bios_en = svm_disabled_by_bios();
		has_cpu = cpu_has_svm_support();
		printf("You have amd cpu\n");
		break;
	default:
		printf("Cannot know cpu type\n");
                return DONT_KNOW_CPU_ERR;
	}
	
	ret |= (bios_en << 1) + (has_cpu << 2);

	printf("Your bios is %s\n", bios_en? "capable":"incapable");
	printf("Your cpu is virtualization %s\n", has_cpu? "capable":"incapable");

	return ret;
}
