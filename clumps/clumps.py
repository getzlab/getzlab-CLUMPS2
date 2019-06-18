import random
import time
import argparse
import gzip
import os

from mapper import GPmapper

from multiprocessing import Process, Queue
from samplers.UniformSampler import *
#from samplers.CoverageSampler import *
#from samplers.MutspecCoverageSampler import *

from utils import hill, parse_resmap, load_mut_freqs, wap
from utils import get_distance_matrix, transform_distance_matrix, get_pdb_muts_overlap, map_pos_with_weights

def main():
    parser = argparse.ArgumentParser(description='Run CLUMPS.')
    parser.add_argument('-d','--muts', required=True, type=str, help='<Required> Directory of files titled with Uniprot IDs that have mutation information')
    parser.add_argument('-m','--maps', required=True, type=str, help='<Required> File mapping uniprot ID to PDB ID with residue-level mapping information.')
    parser.add_argument('-f', '--mut_freq', required=True, help='<Required> Mutational frequenices of patient samples.')
    parser.add_argument('--max_rand', default=10000000, required=False, type=int, help='Maximum number of random samples.')
    parser.add_argument('--mut_types', required=False, default=['M'], nargs='+', type=str, help='Mutation Types (N = nonsense, S = synonymous, M = mutation)')
    parser.add_argument('-u','--sampler', required=False, default='UniformSampler', help='Sampler to use for Null Model.')
    parser.add_argument('-s', '--mut_spectra', required=False, help='Mutational spectra of patient samples.')
    parser.add_argument('-e','--hill_exp', required=False, default=4, type=int, help='Hill Exponent')
    parser.add_argument('-p','--pancan_factor', required=False, default=1.0, type=float, help='Pan Cancer factor, value between 0 - 1. 1.0 if tumor type specified.')
    parser.add_argument('-t','--tumor_type', required=False, default='PanCan', type=str, help='Tumor type.')
    parser.add_argument('--xpo', required=False, default=[3, 4.5, 6, 8, 10], type=list, help='Soft threshold parameter for truncated Gaussian.')
    parser.add_argument('--cores', required=False, type=int, default=1, help='Number of cores.')
    parser.add_argument('--threads', required=False, type=int, default=1, help='Number of threads for sampling.')
    parser.add_argument('--use_provided_values', required=False, default=0, type=int)
    parser.add_argument('--coverage_track', required=False, default=None, type=str, help='Coverage track for null sampler.')
    parser.add_argument('-o', '--out_dir', required=False, default='./res/', type=str, help='Output directory.')

    args = parser.parse_args()

    if args.tumor_type == 'PanCan':
        args.tumor_type = None

    if args.tumor_type and args.pancan_factor != 1.0:
        print('WARNING: args.pancan_factor is not 1 althought args.tumor_type is set. Correcting to args.pancan_factor=1')
        args.pancan_factor = 1.0

    args.mut_types = set(args.mut_types)
    TIMED = True

    if args.sampler is not 'UniformSampler':
        print("Building mapper...")
        gpm = GPmapper()

    with gzip.open(args.maps, 'r') as f:
        for idx,line in enumerate(f):
            u1,u2,pdbch,alidt,resmap = line.decode('utf-8').strip('\n').split('\t', 4)

            if os.path.isfile(os.path.join(args.muts, u1)):
                pdbch = pdbch.split('-')

                ur,pr,prd = parse_resmap(resmap)

                if len(ur) < 5:
                    ## number of mapped residues (between uniprot and pdb)
                    #fo.write('#\n')
                    #fo.close()
                    continue

                # Load mutational frequencies
                mfreq = load_mut_freqs(args.mut_freq)

                # Load Protein file
                protein_muts = map_pos_with_weights(args.muts, u1, mfreq, args.tumor_type, args.mut_types, args.use_provided_values, args.mut_freq)

                # mi: index of mutated residue
                # mv: normalized mutation count at each residue
                # mt: cancer types contributing mutations
                mi,mv,mt = get_pdb_muts_overlap(ur, protein_muts, args.hill_exp, args.use_provided_values)

                if len(mi) > 0:
                    # Get AA residue Coordinates
                    try:
                        D,x = get_distance_matrix(pdbch, pdb_resids=pr)
                    except:
                        print("Unable to load PDB...")
                        continue

                    # Transform distance matrix
                    DDt = transform_distance_matrix(D, ur, args.xpo)

                    print("Sampling {} | {} - {}".format(u1, pdbch, mi))

                    ## matrix that holds mv[i]*mv[j] values (sqrt or not)
                    Mmv = []
                    mvcorr = range(len(mv))

                    for i in range(len(mi)):
                        mrow = sp.zeros(len(mi), sp.float64)
                        for j in range(len(mi)):
                            #mrow[j] = sp.sqrt(mv[i]*mv[j])  ## geometric mean; actually does not perform better in most cases
                            if args.pancan_factor == 1.0:
                                mrow[j] = mv[i]*mv[j]
                            else:
                                mrow[j] = (args.pancan_factor + (1.0-args.pancan_factor)*(len(mt[i] & mt[j])>0)) * mv[i]*mv[j]          ## product
                        Mmv.append(mrow)

                    ## Compute WAP score
                    wap_obs = wap(mi, mvcorr, Mmv, DDt)

                    ## Create Null Model
                    rnd = 0
                    P = [0]*len(args.xpo)
                    WAP_RND = [0]*len(args.xpo)
                    mireal = [i for i in mi]

                    if args.sampler == 'UniformSampler':
                        sam = UniformSampler(ur)
                    elif args.sampler == 'CoverageSampler':
                        sam = CoverageSampler(ur, u1, args.coverage_track, gpm)
                    elif args.sampler == 'MutspecCoverageSampler':
                        sam = MutspecCoverageSampler(ur, u1, args.coverage_track, args.mut_spectra)
                        sam.calcMutSpecProbs(md)

                    if TIMED:
                        STARTTIME=time.time()

                    def rndThread(qu):
                        def booster():
                            """
                            Implements the booster algorithm by Getz et al. that saves CPU time.
                            Returns False if the randomization should be stopped.
                            """
                            ret = False
                            for i in range(len(args.xpo)):
                                s = (rnd - p[i] + 1.0) / ((rnd + 3)*(p[i] + 1))
                                if s >= 0.05271:  ## =0.9/(2*1.96)]**2:
                                    ret = True
                                    break
                            return ret

                        sp.random.seed()
                        p = [0]*len(args.xpo)
                        wap_rnd = [0]*len(args.xpo)
                        rnd = 0

                        exitstatus=0  ## 0 means terminated OK, 1 means it had to abort due to timeout
                        while rnd < args.max_rand/args.threads and (rnd%1000 or booster()):  ## booster is applied once per 1000 randomizations
                            if not rnd%1000 and TIMED and (time.time()-STARTTIME)/60.0 > TIMED:
                                exitstatus=1
                                break
                            x = None

                            while x is None:
                                ## some samplers will fail to yield a sample in some (small number of) of runs due to combinatorics
                                x = sam.sample(mireal)

                            mi,mvcorr = x
                            r = wap(mi, mvcorr, Mmv, DDt)

                            for rr in range(len(args.xpo)):
                                wap_rnd[rr] += r[rr]
                                if r[rr] >= wap_obs[rr]:
                                    p[rr] += 1
                            rnd += 1

                        qu.put((rnd,p,wap_rnd,exitstatus))

                    queue = Queue()
                    pcs = []
                    for r in range(args.threads):
                        x = Process(target=rndThread, args=(queue,))
                        x.start()
                        pcs.append(x)

                    for x in pcs:
                        x.join()

                    totalrnd = 0
                    totalexitstatus = 0
                    for r in range(args.threads):
                        rnd,p,wap_rnd,exitstatus = queue.get()
                        totalrnd += rnd
                        totalexitstatus += exitstatus
                        for i in range(len(args.xpo)):
                            P[i] += p[i]
                            WAP_RND[i] += wap_rnd[i]

                    SHARD = args.maps.rsplit('.',1)[0].rsplit('_',1)[1]
                    with open(os.path.join(args.out_dir, '%s-%d_%s_%s_%s-%s_%s' % (SHARD, idx, u1, u2, pdbch[0], pdbch[1], resmap.split(':',1)[0])), 'a') as f:
                        f.write('\t'.join(['%d/%d' % (P[i], totalrnd) for i in range(len(P))]) + '\n')
                        f.write('#%d\n' % totalexitstatus)

if __name__ == "__main__":
	main()