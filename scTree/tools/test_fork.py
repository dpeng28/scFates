import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"

import numpy as np
import pandas as pd
from functools import partial
from anndata import AnnData
import shutil
import sys
import copy
import igraph
import warnings
from functools import reduce
from statsmodels.stats.multitest import multipletests
import statsmodels.formula.api as sm

from joblib import delayed, Parallel
from tqdm import tqdm
from scipy import sparse

from .. import logging as logg
from .. import settings


try:
    from rpy2.robjects import pandas2ri, Formula
    from rpy2.robjects.packages import importr
    import rpy2.rinterface
    pandas2ri.activate()
    
except ImportError:
    raise RuntimeError(
        'Cannot compute gene expression trends without installing rpy2. \
        \nPlease use "pip3 install rpy2" to install rpy2'
    )

        
if not shutil.which("R"):
    raise RuntimeError(
        "R installation is necessary for computing gene expression trends. \
        \nPlease install R and try again"
    )

try:
    rmgcv = importr("mgcv")
except embedded.RRuntimeError:
    raise RuntimeError(
        'R package "mgcv" is necessary for computing gene expression trends. \
        \nPlease install gam from https://cran.r-project.org/web/packages/gam/ and try again'
    )
rmgcv = importr("mgcv")
rstats = importr("stats")


def test_fork(
    adata: AnnData,
    root,
    leaves,
    n_jobs: int = 1,
    n_map: int = 1,
    n_map_up: int = 1,
    copy: bool = False,
    ):
    
    adata = adata.copy() if copy else adata
    
    logg.info("testing fork")

    genes=adata.var_names[adata.var.signi]
    
    r = adata.uns["tree"]
    g = igraph.Graph.Adjacency((r["B"]>0).tolist(),mode="undirected")
    # Add edge weights and node labels.
    g.es['weight'] = r["B"][r["B"].nonzero()]

    vpath = g.get_shortest_paths(root,leaves)
    interPP = list(set(vpath[0]) & set(vpath[1])) 
    vpath = g.get_shortest_paths(r["pp_info"].loc[interPP,:].time.idxmax(),leaves)

    fork_stat=list()
    upreg_stat=list()
    
    for m in range(n_map):
        logg.info("    mapping: "+str(m))
        ## Diff expr between forks
        
        df = r["pseudotime_list"][str(m)]
        def get_branches(i):
            x = vpath[i]
            segs=r["pp_info"].loc[x,:].seg.unique()
            df_sub=df.loc[df.seg.isin(segs),:].copy(deep=True)
            df_sub.loc[:,"i"]=i
            return(df_sub)

        brcells = pd.concat(list(map(get_branches,range(len(vpath)))),axis=0,sort=False)
        matw=None
        if matw is None:
            brcells["w"]=1
        else:
            brcells["w"] = matw[gene,:][:,r["cells_fitted"]]

        brcells.drop(["seg","edge"],axis=1,inplace=True)

        if sparse.issparse(adata.X):
            Xgenes = adata[brcells.index,genes].X.A.T.tolist()
        else:
            Xgenes = adata[brcells.index,genes].X.T.tolist()

        data = list(zip([brcells]*len(Xgenes),Xgenes))

        stat = Parallel(n_jobs=n_jobs)(
                delayed(gt_fun)(
                    data[d]
                )
                for d in tqdm(range(len(data)),file=sys.stdout,desc="    differential expression")
            )

        fork_stat = fork_stat + [stat]
        
        ## test for upregulation
        logg.info("    test for upregulation for each leave vs root")
        leaves_stat=list()
        for leave in leaves:
            vpath = g.get_shortest_paths(root,leave)
            totest = get_branches(0)

            if sparse.issparse(adata.X):
                Xgenes = adata[totest.index,genes].X.A.T.tolist()
            else:
                Xgenes = adata[totest.index,genes].X.T.tolist()

            data = list(zip([totest]*len(Xgenes),Xgenes))

            stat = Parallel(n_jobs=1)(
                    delayed(test_upreg)(
                        data[d]
                    )
                    for d in tqdm(range(len(data)),file=sys.stdout,desc="    leave "+str(leave))
                )
            stat=pd.DataFrame(stat,index=genes,columns=[str(leave)+"_A",str(leave)+"_p"])
            leaves_stat=leaves_stat+[stat]
            
        upreg_stat=upreg_stat+[pd.concat(leaves_stat,axis=1)]
    
    
    # summarize fork statistics
    fork_stat=list(map(lambda x: pd.DataFrame(x,index=genes,columns=["effect","p_val"]),fork_stat))

    fdr_l=list(map(lambda x: pd.Series(multipletests(x.p_val,method="bonferroni")[1],
                                       index=x.index,name="fdr"),fork_stat))

    st_l=list(map(lambda x: pd.Series((x.p_val<5e-2).values*1,
                  index=x.index,name="signi_p"),fork_stat))
    stf_l=list(map(lambda x: pd.Series((x<5e-2).values*1,
                  index=x.index,name="signi_fdr"),fdr_l))

    fork_stat=list(map(lambda w,x,y,z: pd.concat([w,x,y,z],axis=1),fork_stat,fdr_l,st_l,stf_l))

    effect=pd.concat(list(map(lambda x: x.effect,fork_stat)),axis=1).median(axis=1)
    p_val=pd.concat(list(map(lambda x: x.p_val,fork_stat)),axis=1).median(axis=1)
    fdr=pd.concat(list(map(lambda x: x.fdr,fork_stat)),axis=1).median(axis=1)
    signi_p=pd.concat(list(map(lambda x: x.signi_p,fork_stat)),axis=1).mean(axis=1)
    signi_fdr=pd.concat(list(map(lambda x: x.signi_fdr,fork_stat)),axis=1).mean(axis=1)

    
    colnames=fork_stat[0].columns
    fork_stat=pd.concat([effect,p_val,fdr,signi_p,signi_fdr],axis=1)
    fork_stat.columns=colnames
    
    # summarize upregulation stats
    colnames=upreg_stat[0].columns
    upreg_stat=pd.concat(list(map(lambda i: 
                                  pd.concat(list(map(lambda x: 
                                                     x.iloc[:,i]
                                                     ,upreg_stat)),axis=1).median(axis=1),
                                  range(4))),axis=1)
    upreg_stat.columns=colnames
    
    
    summary_stat=pd.concat([fork_stat,upreg_stat],axis=1)
    
    logg.info("    finished", time=True, end=" " if settings.verbosity > 2 else "\n")
    name=str(root)+"to"+str(leaves[0])+"vs"+str(leaves[1])
    adata.uns["tree"][name] = summary_stat
    logg.hint(
        "added \n"
        "    'tree/"+name+"', DataFrame with fork test results (adata.uns)")

    return adata if copy else None


def gt_fun(data):

    sdf = data[0]
    sdf["exp"] = data[1]

    ## add weighted matrix part
    #
    #

    global rmgcv
    global rstats


    def gamfit(sdf):
        m = rmgcv.gam(Formula("exp ~ s(t)+s(t,by=as.factor(i))+as.factor(i)"),
                      data=sdf,weights=sdf["w"])
        return rmgcv.summary_gam(m)[3][1]

    g = sdf.groupby("i")
    return [g.apply(lambda x: np.mean(x.exp)).diff(periods=-1)[0],gamfit(sdf)]


def test_upreg(data):

    sdf = data[0]
    sdf["exp"] = data[1]

    result = sm.ols(formula="exp ~ t", data=sdf).fit()
    return([result.params["t"],result.pvalues["t"]])



def branch_specific(
    adata: AnnData,
    root,
    leaves,
    effect_b1: float = None,
    effect_b2: float = None,
    stf_cut: float = 0.7,
    pd_a: float = 0,
    pd_p: float = 5e-2,
    copy: bool = False,
    ):
    
    adata = adata.copy() if copy else adata
    
    name=str(root)+"to"+str(leaves[0])+"vs"+str(leaves[1])
    stats = adata.uns["tree"][name]
    
    stats["branch"] = 0 
    
    idx_a=stats.apply(lambda x: (x.effect > effect_b1) &
                                        (x[5]>pd_a) & (x[6]<pd_p) & (x.signi_fdr >stf_cut),
                                         axis=1)
    
    idx_a=idx_a[idx_a].index
    
    logg.info("    "+str(len(idx_a))+" features found to be specific to leave "+str(leaves[0]))
    
    

    idx_b=stats.apply(lambda x: (x.effect < -effect_b2) &
                                            (x[7]>pd_a) & (x[8]<pd_p) & (x.signi_fdr >stf_cut),
                                             axis=1)
    
    idx_b=idx_b[idx_b].index
    
    logg.info("    "+str(len(idx_b))+" features found to be specific to leave "+str(leaves[1]))

    stats.loc[idx_a,"branch"] = leaves[0]
    stats.loc[idx_b,"branch"] = leaves[1]
    
    adata.uns["tree"][str(root)+"to"+str(leaves[0])+"vs"+str(leaves[1])] = stats
    
    
    logg.info("    finished", time=True, end=" " if settings.verbosity > 2 else "\n")
    logg.hint(
        "updated \n"
        "    'tree/"+name+"', DataFrame with additionnal 'branch' column (adata.uns)")

    return adata if copy else None