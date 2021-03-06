# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:light
#     text_representation:
#       extension: .py
#       format_name: light
#       format_version: '1.5'
#       jupytext_version: 1.9.1
#   kernelspec:
#     display_name: baseball_env
#     language: python
#     name: baseball_env
# ---

# # Imports

# +
# %load_ext autoreload
# %autoreload 2

from collections import defaultdict
import os
import sys

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

# Plotting style
plt.style.use('seaborn-colorblind')
mpl.rcParams['axes.grid'] = False
mpl.rcParams['axes.spines.top'] = False
mpl.rcParams['axes.spines.right'] = False
mpl.rcParams['figure.facecolor'] = 'white'
mpl.rcParams['axes.facecolor'] = 'white'

wd = os.path.abspath(os.getcwd())
path_parts = wd.split(os.sep)

p = path_parts[0]
for part in path_parts[1:]:
    p = p + os.sep + part
    if p not in sys.path:
        sys.path.append(p)
        
DATA_PATH = '../data'
MODEL_PATH = '../models'

from retrosheet_tools import get_gamelogs
from shap_explainer import InputLevels, SHAPExplainer
from modules import MLBExplainerModel
from training_utils import lr_schedule, set_lr, loss_fxn, baseline_penalty
# -

# # Load data

# ## From online

df = get_gamelogs(range(2000,2020))
df.to_pickle(f'{DATA_PATH}/df_gamelogs.pk')

# ## From pkl

df = pd.read_pickle(f'{DATA_PATH}/df_gamelogs.pk')
df.info()

# # Inputs and Targets

# +
# Define columns
inputs = [
    'Year', 'Month', 'Day', 'DayNight',
    'ParkID', 'Attendance', 'HmTmLg',
    'UmpHID',
    'VisTmYr', 'VisStPchID', 'VisMgrID',
    'HmTmYr', 'HmStPchID', 'HmMgrID',
]

target_suffixes = ['Runs', 'S', 'D', 'T', 'HR','BB', 'K']
targets = [f'Vis{x}' for x in target_suffixes] + [f'Hm{x}' for x in target_suffixes]

# Select columns
df_subset = df[inputs + targets].copy()

# Bucket attendence
def bucket_attendence(group):
    """Bucket attendence into three groups: High | Med | Low"""
    attendance = group['Attendance']
    capacity = np.max(attendance)
    prec_capacity = attendance/(capacity+1)
    bucket = (prec_capacity//(1/3))
    return bucket

capacity_map = {0: 'low', 1: 'med', 2: 'high'}
df_subset['Attendance'] = df_subset.groupby(['ParkID']).apply(bucket_attendence).values
df_subset['Attendance'] = np.vectorize(capacity_map.get)(df_subset['Attendance'])


# Add win targets
target_suffixes += ['Win']
targets += ['VisWin', 'HmWin']
df_subset['VisWin'] = df_subset['VisRuns'] > df_subset['HmRuns']
df_subset['HmWin'] = df_subset['HmRuns'] >= df_subset['VisRuns']

# Drop NAs
df_subset = df_subset.dropna(how='any', axis='rows')

print(f'''Vis Win {np.mean(df_subset['VisWin']):.2%}''')
print(f'''Hm  Win {np.mean(df_subset['HmWin']):.2%}''')
# -

# ## Create InputLevels

min_obs = 30
input_levels = {
    'years': InputLevels(df_subset['Year'].values, min_obs),
    'months': InputLevels(df_subset['Month'].values, min_obs),
    'dow': InputLevels(df_subset['Day'].values, min_obs),
    'time_of_day': InputLevels(df_subset['DayNight'].values, min_obs),
    'parks': InputLevels(df_subset['ParkID'].values, min_obs),
    'leagues': InputLevels(df_subset['HmTmLg'].values, min_obs),
    'umps': InputLevels(df_subset['UmpHID'].values, min_obs),
    'teams': InputLevels(np.concatenate((df_subset['VisTmYr'].values, df_subset['HmTmYr'].values)), min_obs),
    'pitchers': InputLevels(np.concatenate((df_subset['VisStPchID'].values, df_subset['HmStPchID'].values)), min_obs),
    'managers': InputLevels(np.concatenate((df_subset['VisMgrID'].values, df_subset['HmMgrID'].values)), min_obs),
    'home_away': InputLevels([
        'away_low',
        'away_med',
        'away_high',
        'home_low',
        'home_med',
        'home_high',
    ]),
}


def get_inputs(row, home_tm_last=True):
    
    inputs = [
        input_levels['years'].get_indexes(row['Year']),
        input_levels['months'].get_indexes(row['Month']),
        input_levels['dow'].get_indexes(row['Day']),
        input_levels['time_of_day'].get_indexes(row['DayNight']),
        input_levels['parks'].get_indexes(row['ParkID']),
        input_levels['leagues'].get_indexes(row['HmTmLg']),
        input_levels['umps'].get_indexes(row['UmpHID']),
    ]
    
    if home_tm_last:
        inputs.extend([
            input_levels['teams'].get_indexes(row['VisTmYr']),
            input_levels['pitchers'].get_indexes(row['VisStPchID']),
            input_levels['managers'].get_indexes(row['VisMgrID']),
            input_levels['teams'].get_indexes(row['HmTmYr']),
            input_levels['pitchers'].get_indexes(row['HmStPchID']),
            input_levels['managers'].get_indexes(row['HmMgrID']),
            input_levels['home_away'].get_indexes('home_' + row['Attendance']),
        ])

    else:
        inputs.extend([
            input_levels['teams'].get_indexes(row['HmTmYr']),
            input_levels['pitchers'].get_indexes(row['HmStPchID']),
            input_levels['managers'].get_indexes(row['HmMgrID']),
            input_levels['teams'].get_indexes(row['VisTmYr']),
            input_levels['pitchers'].get_indexes(row['VisStPchID']),
            input_levels['managers'].get_indexes(row['VisMgrID']),
            input_levels['home_away'].get_indexes('away_' + row['Attendance']),
        ])

    return np.array(inputs)


# ## Binary targets

def get_binary_targets(df, var_suffix, n_q=10):
    n = len(df)
    
    var_values = np.concatenate((df_subset[f'Vis{var_suffix}'], df_subset[f'Hm{var_suffix}']))
    unique_vals = np.unique(var_values)
    if len(unique_vals) <= n_q:
        quantiles = unique_vals
    else:
        quantiles = np.unique(np.quantile(
            var_values, 
            q=np.linspace(0,1,n_q)
        ))
    quantiles = quantiles[:-1]
    
    vis_targets, hm_targets = np.zeros((n, len(quantiles))), np.zeros((n, len(quantiles)))
    
    for i, q in enumerate(quantiles):
        vis_targets[:,i] = df_subset[f'Vis{var_suffix}'] <= q
        hm_targets[:,i] = df_subset[f'Hm{var_suffix}'] <= q
       
    columns = [f'<= {x:.2f}' for x in quantiles]
    vis_targets = pd.DataFrame(vis_targets, columns=columns)
    hm_targets = pd.DataFrame(hm_targets, columns=columns)
    
    return vis_targets, hm_targets


target_dfs = dict()
for suffix in target_suffixes:
    if suffix != 'Win':
        vis_targets, hm_targets = get_binary_targets(df_subset, suffix, n_q=10)
        target_dfs[suffix] = {'Vis': vis_targets, 'Hm': hm_targets}


# ## Get sample

def get_sample(df, target_dfs, n=None, idxs=None, mask_p=0.15, rnd=None):
    if rnd is None:
        rnd = np.random.RandomState()
    
    if idxs is not None:
        s = df.iloc[idxs]
    else:
        s = df.sample(n=n)
        
    x = np.stack(s.apply(lambda r: get_inputs(r, rnd.rand() <= 0.5), axis=1).tolist())

    vis_hm_idx = np.array([
        ('home' in x) if x is not None else False
        for x 
        in input_levels['home_away'].get_levels(x[:,-1])
    ]).astype(int)
    
    if mask_p > 0:
        mask = np.random.rand(*x.shape) <= mask_p
        x[mask] = 0
        
    # Targets
    row_idx = np.arange(len(s))
    
    targets = {
        'Win': s[['VisWin', 'HmWin']].values[row_idx, vis_hm_idx].astype(np.float32)
    }
    
    for k, v in target_dfs.items():
        v = np.stack(
            (v['Vis'].loc[s.index], v['Hm'].loc[s.index]), 
            axis=-1
        )
        targets[f'{k}_team'] = v[row_idx, :, vis_hm_idx].astype(np.float32)
        targets[f'{k}_opp'] = v[row_idx, :, 1 - vis_hm_idx].astype(np.float32)

    return x, targets


# # Train Model

# +
batch_size = 128
n_batches = 2000
lr_min, lr_max = 0.0001, 0.002
lr_cycle_len = 50
lrs = list(lr_schedule(lr_cycle_len, lr_min, lr_max))*(n_batches//lr_cycle_len)
avg_penalty_scale, baseline_penalty_scale = 1, 5

# Init model and optimizer
model = MLBExplainerModel(
    input_levels=input_levels,
    target_sizes={k: v['Vis'].shape[1] for k, v in target_dfs.items()}
)
optimizer = torch.optim.Adam(params=model.parameters(), lr=lr_min)

# Training loop
losses = []
for i, lr in enumerate(lrs, 1):
    
    # Generate sample
    x, y = get_sample(df_subset, target_dfs, n=batch_size)
    
    # Forward
    model.train()
    yh = model(x)
    
    # Loss
    loss = loss_fxn(yh, y, avg_penalty_scale)
    losses.append(loss.item())
    
    # Baseline penality
    # Keeps baseline prediction inline with average statistics of data
    x_baseline = np.zeros((1, x.shape[-1]))  # Single observation with all features set to baseline
    model.eval()                             # Turn off batchnorm for a single observation
    yh_baseline = model(x_baseline)
    pentalty = baseline_pentalty(yh_baseline, y, baseline_penalty_scale)
    loss = loss + pentalty
    
    # Backward
    loss.backward()
    set_lr(optimizer, lr)
    optimizer.step()
            
    # Clean up
    optimizer.zero_grad()
    
    if i%100 == 0:
        print(f'{i:>6,}: {np.mean(losses[-100:]):>8.4f}')

fig, ax = plt.subplots(figsize=(10,5))
_ = ax.plot(pd.Series(losses).rolling(window=100).mean())

torch.save(model.state_dict(), '../models/mlb_explainer.pth')
# -

# # Eval

# ## Load trained model

model = MLBExplainerModel(
    input_levels=input_levels,
    target_sizes={k: v['Vis'].shape[1] for k, v in target_dfs.items()}
)
model.load_state_dict( torch.load('../models/mlb_explainer.pth'))
model.eval()

# ## Eval model on all data

# +
y, yh = defaultdict(list), defaultdict(list)

for i in range(0, len(df_subset)//1000 + 1):
        
    idxs = range(i*1000, min(len(df_subset), (i + 1)*1000))
    x_i, y_i = get_sample(df_subset, target_dfs, idxs=idxs, mask_p=0)

    with torch.no_grad():
        yh_i = model(x_i)
        yh_i = {k: v.numpy() for k, v in yh_i.items()}
        
    for k in y_i.keys():
        y[k].append(y_i[k])
        if k in yh_i:
            yh[k].append(yh_i[k])
        
    print(i, end=' ')

y = {k: np.concatenate(v, axis=0) for k, v in y.items()}
yh = {k: np.concatenate(v, axis=0) for k, v in yh.items()}
# -

# ## Win Probability

# Win probability
fig, ax = plt.subplots(figsize=(10,5))
_ = ax.hist(yh['Win'])

# +
outcome = y['Win']
yh_proba = yh['Win']

outcome_avg = np.mean(outcome)
proba_avg = np.mean(yh_proba)
pred_avg = np.mean(yh_proba >= 0.5)
accuracy = np.mean(outcome == (yh_proba >= 0.5))
print(f'''Data set's win probability : {outcome_avg:.2%}''')
print(f'''Model's predicted proba    : {proba_avg:.2%}''')
print(f'''Model's predicted win proba: {pred_avg:.2%}''')
print(f'''Model's prediction accuracy: {accuracy:.2%}''')

# +
from sklearn.metrics import precision_recall_fscore_support

p,r,f = [], [], []
thresholds = np.linspace(min(yh_proba), max(yh_proba), 20)
for t in thresholds:
    precision, recall, fscore, _ = precision_recall_fscore_support(outcome
                                                                   , yh_proba >= t
                                                                   , zero_division=0
                                                                  )
    p.append(precision[1])
    r.append(recall[1])
    f.append(fscore[1])

fig, axs = plt.subplots(figsize=(10, 10), nrows=2)

axs[0].plot(r, p, '-o')
axs[0].set_title(label='Precision-Recall Curve', loc='left', fontdict={'fontsize': 16})
axs[0].set_xlabel('Recall', fontsize=16)
axs[0].set_ylabel('Pecision', fontsize=16)

axs[1].plot(thresholds[1:-1], f[1:-1], '-o')
axs[1].set_title(label='F-score Curve', loc='left', fontdict={'fontsize': 16})
axs[1].set_xlabel('Prediction Threshold', fontsize=16)
axs[1].set_ylabel('F-Score', fontsize=16)

# +
from sklearn.calibration import calibration_curve

fig, ax = plt.subplots(figsize=(8, 8))
ax.plot([0, 1], [0, 1], "k:", label="Perfectly calibrated")

fraction_of_positives, mean_predicted_value = calibration_curve(outcome, yh_proba, n_bins=20)
ax.plot(mean_predicted_value, fraction_of_positives, 's-', label='Model')

ax.set_xlabel("Mean predicted value")
ax.set_ylabel("Fraction of positives")
ax.set_ylim([-0.05, 1.05])
ax.legend(loc="lower right")
ax.set_title('Calibration plot')
# -

# # SHAP

input_features = [
    'year',
    'month',
    'dow',
    'time_of_day',
    'park',
    'league',
    'ump',
    'opp_team',
    'opp_pitcher',
    'opp_manager',
    'team',
    'pitcher',
    'manager',
    'home_away',
]


# ## Model Wrappers

@torch.no_grad()
def predict_win_prob(x):
    model.eval()
    yhs = model(x)
    return yhs['Win'].numpy()


# ## Explainers

win_prob_explainer = SHAPExplainer(
    prediction_fxn=predict_win_prob,
    baseline_values = np.zeros(len(input_features)),
    var_names=input_features
)
win_prob_explainer.yh_baseline

# ## Explain each game

# +
# x, targets = get_sample(df_subset, target_dfs, n=5000, mask_p=0)
x, y = get_sample(
    df_subset, 
    target_dfs,
    idxs=range(len(df_subset)),
    mask_p=0,
)

yh = predict_win_prob(x)

x.shape
# -

win_prob_sv = win_prob_explainer.get_shap_values(x)

# ## Global feature impact

fig, ax = win_prob_explainer.plot_global_impact(
    shap_values=win_prob_sv, 
    relative=False, 
    target_name="Win Probability"
)
_ = ax.set_xticklabels([f'{x:.0%}' for x in ax.get_xticks()])

# ## Feature levels marginal impact

# +
feat_name = 'team'
col_idx, *_ = np.where(np.array(win_prob_explainer.var_names) == feat_name)
col_idx = col_idx.item()
feat_values = input_levels['teams'].get_levels(x[:, col_idx])

print(f'''Median win probability of 2019 Red Sox: {np.median(yh[feat_values == 'BOS2019']):.2%}''')

fig, ax = win_prob_explainer.plot_feature_levels_marginal_impact(
    feat_values=feat_values,
    feat_shap_values=win_prob_sv[:, col_idx],
    model_predictions=predict_win_prob(x),
    yh_ref=win_prob_explainer.yh_baseline,
    n_label=5
)

_ = ax.set_title(
    label='Marginal impact of each team vs. Model prediction for game outcome\nMedian across all HOME games played by team', 
    loc='left', 
    fontdict={'fontsize': 16}
)

_ = ax.set_xticklabels([f'{x:.0%}' for x in ax.get_xticks()])
_ = ax.set_yticklabels([f'{x:.0%}' for x in ax.get_yticks()])
# -

# ## Feature embedding plot

# +
feat_name = 'home_away'
col_idx, *_ = np.where(np.array(win_prob_explainer.var_names) == feat_name)
col_idx = col_idx.item()
feat_values = input_levels['home_away'].get_levels(x[:, col_idx])

level_embeddings = model.embedders['home_away'].weight.data.numpy()

fig, ax = win_prob_explainer.plot_feature_level_embeddings(
    level_embeddings=level_embeddings,
    emb_idx_level_mapper=input_levels['home_away'].get_levels,
    feat_values=feat_values,
    feat_shap_values=win_prob_sv[:, col_idx],
    n_label=5,
)

# +
feat_name = 'pitcher'
col_idx, *_ = np.where(np.array(win_prob_explainer.var_names) == feat_name)
col_idx = col_idx.item()
feat_values = input_levels['pitchers'].get_levels(x[:, col_idx])

level_embeddings = model.embedders['pitchers'].weight.data.numpy()

fig, ax = win_prob_explainer.plot_feature_level_embeddings(
    level_embeddings=level_embeddings,
    emb_idx_level_mapper=input_levels['pitchers'].get_levels,
    feat_values=feat_values,
    feat_shap_values=win_prob_sv[:, col_idx],
    n_label=5,
)
# -

# ## Local feature impact

fig, ax = win_prob_explainer.plot_local_impact(
    obs_shap_values=win_prob_sv[-1],
    yh_ref=win_prob_explainer.yh_baseline,
    target_name="Win Probability"
)



