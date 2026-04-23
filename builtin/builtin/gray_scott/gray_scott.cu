// gray_scott.cu
// MPI + CUDA Gray-Scott reaction-diffusion simulation
//
// PDE:
//   du/dt = Du*∇²u - u·v² + F·(1 - u)
//   dv/dt = Dv*∇²v + u·v² - (F+k)·v
//
// Domain decomposition: rows partitioned across MPI ranks.
// Periodic BC in X; MPI halo exchange in Y.
// Outputs u field to HDF5 every --out-every steps.

#include <mpi.h>
#include <cuda_runtime.h>
#include <hdf5.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <vector>
#include <string>

// ---------------------------------------------------------------------------
// CUDA kernel
// ---------------------------------------------------------------------------
__global__ void gs_step(
    const float* __restrict__ u, const float* __restrict__ v,
    float*       __restrict__ un, float*      __restrict__ vn,
    int W, int H,          // H = local_H + 2 ghost rows
    float Du, float Dv, float F, float k, float dt)
{
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y + 1;  // skip ghost row 0
    if (x >= W || y >= H - 1) return;

    // Periodic in X, ghost-row bounded in Y
    int xp = y * W + (x + 1 < W ? x + 1 : 0);
    int xm = y * W + (x - 1 >= 0 ? x - 1 : W - 1);
    int yp = (y + 1) * W + x;
    int ym = (y - 1) * W + x;
    int c  = y * W + x;

    float uc = u[c], vc = v[c];
    float lap_u = u[xm] + u[xp] + u[ym] + u[yp] - 4.f * uc;
    float lap_v = v[xm] + v[xp] + v[ym] + v[yp] - 4.f * vc;
    float uvv   = uc * vc * vc;

    un[c] = uc + dt * (Du * lap_u - uvv + F * (1.f - uc));
    vn[c] = vc + dt * (Dv * lap_v + uvv - (F + k) * vc);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
static void cuda_check(cudaError_t err, const char* ctx) {
    if (err != cudaSuccess) {
        fprintf(stderr, "CUDA error [%s]: %s\n", ctx, cudaGetErrorString(err));
        MPI_Abort(MPI_COMM_WORLD, 1);
    }
}

static void write_hdf5(const char* dir, const float* data, int GW, int GH, int step) {
    char path[512];
    snprintf(path, sizeof(path), "%s/gs_%06d.h5", dir, step);
    hid_t fid = H5Fcreate(path, H5F_ACC_TRUNC, H5P_DEFAULT, H5P_DEFAULT);
    hsize_t dims[2] = {(hsize_t)GH, (hsize_t)GW};
    hid_t sid = H5Screate_simple(2, dims, NULL);
    hid_t did = H5Dcreate2(fid, "u", H5T_NATIVE_FLOAT, sid,
                            H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
    H5Dwrite(did, H5T_NATIVE_FLOAT, H5S_ALL, H5S_ALL, H5P_DEFAULT, data);
    H5Dclose(did); H5Sclose(sid); H5Fclose(fid);
    printf("  wrote %s\n", path);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
int main(int argc, char** argv)
{
    MPI_Init(&argc, &argv);
    int rank, nranks;
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &nranks);

    // Defaults (Spots pattern)
    int   GW = 512, GH = 512, steps = 5000, out_every = 500;
    float Du = 0.16f, Dv = 0.08f, F = 0.035f, k = 0.060f, dt = 1.0f;
    const char* outdir = ".";

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--width"))    GW       = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--height"))   GH       = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--steps"))    steps    = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--out-every"))out_every= atoi(argv[++i]);
        else if (!strcmp(argv[i], "--outdir"))   outdir   = argv[++i];
        else if (!strcmp(argv[i], "--Du"))       Du       = atof(argv[++i]);
        else if (!strcmp(argv[i], "--Dv"))       Dv       = atof(argv[++i]);
        else if (!strcmp(argv[i], "--F"))        F        = atof(argv[++i]);
        else if (!strcmp(argv[i], "--k"))        k        = atof(argv[++i]);
        else if (!strcmp(argv[i], "--dt"))       dt       = atof(argv[++i]);
    }

    // Row-wise domain decomposition
    int base_H  = GH / nranks;
    int extra   = GH % nranks;
    int local_H = base_H + (rank < extra ? 1 : 0);
    int y_start = rank * base_H + (rank < extra ? rank : extra);

    int  buf_H = local_H + 2;                     // +2 ghost rows
    size_t bytes = (size_t)GW * buf_H * sizeof(float);

    std::vector<float> h_u(GW * buf_H, 1.f);
    std::vector<float> h_v(GW * buf_H, 0.f);

    // Seed a 40×40 patch of v in the global centre
    for (int gy = GH / 2 - 20; gy < GH / 2 + 20; gy++) {
        int ly = gy - y_start + 1;               // +1 because row 0 is ghost
        if (ly >= 1 && ly <= local_H) {
            for (int gx = GW / 2 - 20; gx < GW / 2 + 20; gx++) {
                h_u[ly * GW + gx] = 0.5f;
                h_v[ly * GW + gx] = 0.25f;
            }
        }
    }

    float *d_u[2], *d_v[2];
    cuda_check(cudaMalloc(&d_u[0], bytes), "malloc u0");
    cuda_check(cudaMalloc(&d_u[1], bytes), "malloc u1");
    cuda_check(cudaMalloc(&d_v[0], bytes), "malloc v0");
    cuda_check(cudaMalloc(&d_v[1], bytes), "malloc v1");
    cuda_check(cudaMemcpy(d_u[0], h_u.data(), bytes, cudaMemcpyHostToDevice), "H2D u");
    cuda_check(cudaMemcpy(d_v[0], h_v.data(), bytes, cudaMemcpyHostToDevice), "H2D v");

    dim3 block(16, 16);
    dim3 grid((GW + 15) / 16, (local_H + 15) / 16);

    int prev = (rank - 1 + nranks) % nranks;
    int next = (rank + 1) % nranks;

    std::vector<float> g_u;
    std::vector<int> counts(nranks), displs(nranks);
    if (rank == 0) {
        g_u.resize(GW * GH);
        for (int r = 0; r < nranks; r++) {
            int rH = base_H + (r < extra ? 1 : 0);
            int rY = r * base_H + (r < extra ? r : extra);
            counts[r] = GW * rH;
            displs[r] = GW * rY;
        }
    }

    if (rank == 0)
        printf("Gray-Scott %dx%d  %d ranks  %d steps\n", GW, GH, nranks, steps);

    std::vector<float> row(GW);

    for (int s = 0; s < steps; s++) {
        int cur = s & 1, nxt = 1 - cur;

        // -- Halo exchange (u) --
        std::vector<float> st(GW), sb(GW), rt(GW), rb(GW);
        cuda_check(cudaMemcpy(st.data(), d_u[cur] + 1 * GW,           GW * sizeof(float), cudaMemcpyDeviceToHost), "d2h top u");
        cuda_check(cudaMemcpy(sb.data(), d_u[cur] + local_H * GW,     GW * sizeof(float), cudaMemcpyDeviceToHost), "d2h bot u");
        MPI_Sendrecv(st.data(), GW, MPI_FLOAT, prev, 0, rb.data(), GW, MPI_FLOAT, next, 0, MPI_COMM_WORLD, MPI_STATUS_IGNORE);
        MPI_Sendrecv(sb.data(), GW, MPI_FLOAT, next, 1, rt.data(), GW, MPI_FLOAT, prev, 1, MPI_COMM_WORLD, MPI_STATUS_IGNORE);
        cuda_check(cudaMemcpy(d_u[cur],                       rt.data(), GW * sizeof(float), cudaMemcpyHostToDevice), "h2d top u");
        cuda_check(cudaMemcpy(d_u[cur] + (local_H + 1) * GW, rb.data(), GW * sizeof(float), cudaMemcpyHostToDevice), "h2d bot u");

        // -- Halo exchange (v) --
        cuda_check(cudaMemcpy(st.data(), d_v[cur] + 1 * GW,           GW * sizeof(float), cudaMemcpyDeviceToHost), "d2h top v");
        cuda_check(cudaMemcpy(sb.data(), d_v[cur] + local_H * GW,     GW * sizeof(float), cudaMemcpyDeviceToHost), "d2h bot v");
        MPI_Sendrecv(st.data(), GW, MPI_FLOAT, prev, 2, rb.data(), GW, MPI_FLOAT, next, 2, MPI_COMM_WORLD, MPI_STATUS_IGNORE);
        MPI_Sendrecv(sb.data(), GW, MPI_FLOAT, next, 3, rt.data(), GW, MPI_FLOAT, prev, 3, MPI_COMM_WORLD, MPI_STATUS_IGNORE);
        cuda_check(cudaMemcpy(d_v[cur],                       rt.data(), GW * sizeof(float), cudaMemcpyHostToDevice), "h2d top v");
        cuda_check(cudaMemcpy(d_v[cur] + (local_H + 1) * GW, rb.data(), GW * sizeof(float), cudaMemcpyHostToDevice), "h2d bot v");

        // -- Compute step --
        gs_step<<<grid, block>>>(d_u[cur], d_v[cur], d_u[nxt], d_v[nxt],
                                  GW, buf_H, Du, Dv, F, k, dt);
        cuda_check(cudaDeviceSynchronize(), "gs_step");

        // -- Output --
        if ((s + 1) % out_every == 0) {
            std::vector<float> local_u(GW * local_H);
            cuda_check(cudaMemcpy(local_u.data(), d_u[nxt] + GW,
                                  GW * local_H * sizeof(float), cudaMemcpyDeviceToHost), "d2h gather");
            MPI_Gatherv(local_u.data(), GW * local_H, MPI_FLOAT,
                        g_u.data(), counts.data(), displs.data(), MPI_FLOAT,
                        0, MPI_COMM_WORLD);
            if (rank == 0)
                write_hdf5(outdir, g_u.data(), GW, GH, s + 1);
        }
    }

    if (rank == 0) printf("Done.\n");

    cudaFree(d_u[0]); cudaFree(d_u[1]);
    cudaFree(d_v[0]); cudaFree(d_v[1]);
    MPI_Finalize();
    return 0;
}
