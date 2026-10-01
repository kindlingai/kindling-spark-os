// Describe a physical range to the RM as a memory-list object and export it as an opaque fd that
// CUDA can import (cudaExternalMemoryHandleTypeOpaqueFd).
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include "nvtypes.h"
#include "nvmisc.h"
#include "nvos.h"
#include "nvstatus.h"
#include "class/cl0000.h"
#include "class/cl0080.h"
#include "class/cl2080.h"
#include "class/cl84a0.h"
#include "ctrl/ctrl0000/ctrl0000unix.h"
#include "ctrl/ctrl2080/ctrl2080fb.h"
#include "nv-ioctl.h"
#include "nv-ioctl-numbers.h"
#include "nv_escape.h"

static int ctl = -1;
static NvHandle client;
static const NvHandle device = 0x5c000001, subdevice = 0x5c000002;
static NvHandle next_list = 0x5c000010;

static int rm_alloc(NvHandle parent, NvHandle handle, NvU32 cls, void *params, NvU32 size, NvU32 *status)
{
    NVOS21_PARAMETERS p = {
        .hRoot = client, .hObjectParent = parent, .hObjectNew = handle, .hClass = cls,
        .pAllocParms = NV_PTR_TO_NvP64(params), .paramsSize = size,
    };
    if (ioctl(ctl, _IOWR(NV_IOCTL_MAGIC, NV_ESC_RM_ALLOC, NVOS21_PARAMETERS), &p) < 0)
        return -errno;
    if (cls == NV01_ROOT_CLIENT)
        client = p.hObjectNew;
    *status = p.status;
    return 0;
}

static int rm_control(NvHandle object, NvU32 cmd, void *params, NvU32 size, NvU32 *status)
{
    NVOS54_PARAMETERS p = {
        .hClient = client, .hObject = object, .cmd = cmd,
        .params = NV_PTR_TO_NvP64(params), .paramsSize = size,
    };
    if (ioctl(ctl, _IOWR(NV_IOCTL_MAGIC, NV_ESC_RM_CONTROL, NVOS54_PARAMETERS), &p) < 0)
        return -errno;
    *status = p.status;
    return 0;
}

#define CHECK(call, what)                                                          \
    do {                                                                           \
        NvU32 st_ = 0;                                                             \
        int rc_ = (call);                                                          \
        if (rc_ || st_) {                                                          \
            fprintf(stderr, "%s: ioctl %d status 0x%x\n", what, rc_, st_);         \
            return -1;                                                             \
        }                                                                          \
    } while (0)

// Fills *base and *size with the DISPLAY_FRM carveout the RM reports. Returns 0 on success.
int rm_display_frm(unsigned long long *base, unsigned long long *size)
{
    NV2080_CTRL_FB_GET_CARVEOUT_REGION_INFO_PARAMS info = {0};
    NvU32 st;
    if (rm_control(subdevice, NV2080_CTRL_CMD_FB_GET_CARVEOUT_REGION_INFO, &info, sizeof info, &st) || st)
        return -1;
    for (NvU32 i = 0; i < info.numCarveoutRegions; i++)
        if (info.carveoutRegion[i].carveoutType == NV2080_CTRL_FB_GET_CARVEOUT_REGION_CARVEOUT_TYPE_DISPLAY_FRM) {
            *base = info.carveoutRegion[i].base;
            *size = info.carveoutRegion[i].size;
            return 0;
        }
    return -1;
}

// Opens an RM client with a device and subdevice. Returns 0 on success.
int rm_open(void)
{
    ctl = open("/dev/nvidiactl", O_RDWR | O_CLOEXEC);
    int gpu = open("/dev/nvidia0", O_RDWR | O_CLOEXEC);
    if (ctl < 0 || gpu < 0)
        return -1;
    nv_ioctl_rm_api_version_t ver = {.cmd = NV_RM_API_VERSION_CMD_QUERY};
    if (ioctl(ctl, _IOWR(NV_IOCTL_MAGIC, NV_ESC_CHECK_VERSION_STR, ver), &ver) < 0)
        return -1;
    nv_ioctl_register_fd_t reg = {.ctl_fd = ctl};
    if (ioctl(gpu, _IOWR(NV_IOCTL_MAGIC, NV_ESC_REGISTER_FD, reg), &reg) < 0)
        return -1;

    NvU32 st;
    CHECK(rm_alloc(0, 0, NV01_ROOT_CLIENT, NULL, 0, &st_), "alloc client");
    NV0080_ALLOC_PARAMETERS dev = {.deviceId = 0};
    CHECK(rm_alloc(client, device, NV01_DEVICE_0, &dev, sizeof dev, &st_), "alloc device");
    NV2080_ALLOC_PARAMETERS sub = {.subDeviceId = 0};
    CHECK(rm_alloc(device, subdevice, NV20_SUBDEVICE_0, &sub, sizeof sub, &st_), "alloc subdevice");
    (void)st;
    return 0;
}

// Wraps the physical range [base, base+size) in a contiguous system-memory list object and
// exports it. Returns the opaque fd, or -1. If handle is non-NULL, it receives the object's
// handle for rm_free.
int rm_export_range_h(unsigned long long base, unsigned long long size, unsigned int *handle)
{
    NvHandle list = next_list++;
    if (handle)
        *handle = list;
    NvU64 pfn = base >> 12;
    NV_MEMORY_LIST_ALLOCATION_PARAMS p = {
        .pageCount = 1,
        .size = size,
        .limit = size - 1,
        .pageNumberList = NV_PTR_TO_NvP64(&pfn),
        .flagsOs02 = DRF_DEF(OS02, _FLAGS, _PHYSICALITY, _CONTIGUOUS) |
                     DRF_DEF(OS02, _FLAGS, _COHERENCY, _CACHED),
    };
    CHECK(rm_alloc(device, list, NV01_MEMORY_LIST_SYSTEM, &p, sizeof p, &st_), "alloc memory list");

    int fd = open("/dev/nvidiactl", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        return -1;
    NV0000_CTRL_OS_UNIX_EXPORT_OBJECT_TO_FD_PARAMS e = {
        .object = {.type = NV0000_CTRL_OS_UNIX_EXPORT_OBJECT_TYPE_RM,
                   .data.rmObject = {.hDevice = device, .hParent = device, .hObject = list}},
        .fd = fd,
    };
    CHECK(rm_control(client, NV0000_CTRL_CMD_OS_UNIX_EXPORT_OBJECT_TO_FD, &e, sizeof e, &st_), "export to fd");
    return fd;
}

int rm_export_range(unsigned long long base, unsigned long long size)
{
    return rm_export_range_h(base, size, NULL);
}

// Frees an object made by rm_export_range_h. Returns 0 on success.
int rm_free(unsigned int handle)
{
    NVOS00_PARAMETERS p = {.hRoot = client, .hObjectParent = device, .hObjectOld = handle};
    if (ioctl(ctl, _IOWR(NV_IOCTL_MAGIC, NV_ESC_RM_FREE, NVOS00_PARAMETERS), &p) < 0)
        return -errno;
    return p.status;
}
