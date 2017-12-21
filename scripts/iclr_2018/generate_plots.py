from cycler import cycler
import os
import argparse
import numpy as np

from scipy import stats

import matplotlib.pyplot as plt
from dps.parallel.hyper import extract_data_from_job
from dps.utils import process_path, Config, sha_cache


cache_dir = process_path('/home/eric/.cache/dps_plots')
plot_dir = './plots'

single_op_paths = Config()
single_op_paths['sum:dps'] = 'sample_efficiency_single_op/dps/sum/exp_grid_arith_sum_2017_10_23_03_08_50.zip'
single_op_paths['sum:cnn'] = 'sample_efficiency_single_op/cnn/sum/exp_cnn_sum_12hours_2017_10_21_00_54_52.zip'

single_op_paths['prod:dps'] = 'sample_efficiency_single_op/dps/prod/exp_grid_arith_prod_2017_10_23_03_10_48.zip'
single_op_paths['prod:cnn'] = 'sample_efficiency_single_op/cnn/prod/exp_cnn_prod_12hours_2017_10_21_00_55_50.zip'

single_op_paths['max:dps'] = 'sample_efficiency_single_op/dps/max/exp_grid_arith_max_2017_10_23_03_13_24.zip'
single_op_paths['max:cnn'] = 'sample_efficiency_single_op/cnn/max/exp_cnn_max_12hours_2017_10_21_00_56_53.zip'

single_op_paths['min:dps'] = 'sample_efficiency_single_op/dps/min/exp_grid_arith_min_2017_10_23_03_14_42.zip'
single_op_paths['min:cnn'] = 'sample_efficiency_single_op/cnn/min/exp_cnn_min_12hours_2017_10_21_00_57_32.zip'

combined_paths = Config()
combined_paths['dps'] = 'sample_efficiency_combined/dps/exp_grid_arith_all_2017_10_24_05_51_25.zip'
combined_paths['cnn'] = 'sample_efficiency_combined/cnn/exp_cnn_all_2017_10_23_01_22_58.zip'

parity_paths = Config()
parity_paths['B:dps'] = 'curriculum_parity/dps_fixed/B/results.zip'
parity_paths['B:cnn'] = 'curriculum_parity/cnn/A/exp_final_cnn_parity_A_2017_10_26_23_08_20.zip'

parity_paths['C:dps'] = 'curriculum_parity/dps_fixed/C/results.zip'
parity_paths['C:cnn'] = 'curriculum_parity/cnn/B/exp_final_cnn_parity_B_2017_10_26_23_07_11.zip'

size_paths = Config()
size_paths["A:dps"] = ''
size_paths["A:cnn"] = 'curriculum_size/cnn/A/results.zip'

size_paths["B:dps"] = ''
size_paths["B:cnn"] = 'curriculum_size/cnn/B/results.zip'

size_paths["C:dps"] = ''
size_paths["C:cnn"] = 'curriculum_size/cnn/C/results.zip'

size_paths["D:dps"] = ''
size_paths["D:cnn"] = 'curriculum_size/cnn/D/results.zip'

size_paths["E:dps"] = ''
size_paths["E:cnn"] = 'curriculum_size/cnn/E/results.zip'

size_paths["F:dps"] = ''
size_paths["F:cnn"] = 'curriculum_size/cnn/F/results.zip'

ablation_paths = Config()
ablation_paths['full_interface'] = 'sample_efficiency_combined/dps/results.zip'
ablation_paths['no_modules'] = 'ablations/no_modules/results.zip'
ablation_paths['no_transformations'] = 'ablations/no_transformations/results.zip'
ablation_paths['no_classifiers'] = 'ablations/no_classifiers/results.zip'


def std_dev(ys):
    y_upper = y_lower = [_y.std() for _y in ys]
    return y_upper, y_lower


def ci(data, coverage):
    return stats.t.interval(
        coverage, len(data)-1, loc=np.mean(data), scale=stats.sem(data))


def ci95(ys):
    conf_int = [ci(_y.values, 0.95) for _y in ys]
    y = [_y.mean() for _y in ys]
    y_lower = y - np.array([ci[0] for ci in conf_int])
    y_upper = np.array([ci[1] for ci in conf_int]) - y
    return y_upper, y_lower


def std_err(ys):
    y_upper = y_lower = [stats.sem(_y.values) for _y in ys]
    return y_upper, y_lower


spread_measures = {func.__name__: func for func in [std_dev, ci95, std_err]}


@sha_cache(cache_dir)
def _extract_cnn_data(f, n_controller_units, spread_measure, y_func, groupby='n_train'):
    flat = False
    if isinstance(n_controller_units, int):
        n_controller_units = [n_controller_units]
        flat = True

    data = {}
    df = extract_data_from_job(f, ['n_controller_units', groupby])
    groups = df.groupby('n_controller_units')
    for i, (k, _df) in enumerate(groups):
        if k in n_controller_units:
            _groups = _df.groupby(groupby)
            values = list([g for g in _groups if g[0] > 1])
            x = [v[0] for v in values]
            ys = [y_func(v[1]) for v in values]

            y = [_y.mean() for _y in ys]
            y_upper, y_lower = spread_measures[spread_measure](ys)

            data[k] = np.stack([x, y, y_upper, y_lower])

    if flat:
        return next(iter(data.values()))
    return data


@sha_cache(cache_dir)
def _extract_rl_data(f, spread_measure, y_func):
    data = {}
    df = extract_data_from_job(f, 'n_train')
    groups = df.groupby('n_train')
    values = list(groups)
    x = [v[0] for v in values]
    ys = [y_func(v[1]) for v in values]

    y = [_y.mean() for _y in ys]
    y_upper, y_lower = spread_measures[spread_measure](ys)

    data = np.stack([x, y, y_upper, y_lower])
    return data


def gen_sample_efficiency_single_op():
    fig, axes = plt.subplots(2, 2, sharex=True, sharey=True, figsize=(10, 7))

    fig.text(0.52, 0.01, '# Training Examples', ha='center', fontsize=12)
    fig.text(0.01, 0.51, '% Test Error', va='center', rotation='vertical', fontsize=12)

    cnn_n_train = [32, 128, 512]
    spread_measure = 'std_err'

    pp = [
        dict(title='Sum', key='sum'),
        dict(title='Product', key='prod'),
        dict(title='Maximum', key='max'),
        dict(title='Minimum', key='min'),
    ]

    for i, (ax, p) in enumerate(zip(axes.flatten(), pp)):
        label_order = []

        x, y, *yerr = _extract_rl_data(single_op_paths[p['key']]['dps'], spread_measure)
        label = 'RL + Interface'
        ax.errorbar(x, y, yerr=yerr, label=label, ls='--')
        label_order.append(label)

        cnn_sum_data = _extract_cnn_data(single_op_paths[p['key']]['cnn'], cnn_n_train, spread_measure)

        for k, v in cnn_sum_data.items():
            x, y, *yerr = v
            label = "CNN - {} hidden units".format(k)
            ax.errorbar(x, y, yerr=yerr, label=label)
            label_order.append(label)

        ax.set_title(p['title'])
        ax.tick_params(axis='both', labelsize=14)

    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    legend_handles = {l: h for h, l in zip(*ax.get_legend_handles_labels())}
    ordered_handles = [legend_handles[l] for l in label_order]

    ax.legend(ordered_handles, label_order, loc='best', ncol=1)
    plt.subplots_adjust(
        left=0.07, bottom=0.08, right=0.98, top=0.95, wspace=0.05, hspace=0.15)

    plt.savefig(os.path.join(plot_dir, 'sample_efficiency.pdf'))


def gen_sample_efficiency_combined():
    plt.figure(figsize=(5, 3.5))

    ax = plt.gca()

    ax.set_ylabel('% Test Error', fontsize=12)
    ax.set_xlabel('# Training Examples', fontsize=12)
    ax.tick_params(axis='both', labelsize=14)
    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    cnn_n_train = [32, 128, 512]
    spread_measure = 'std_err'

    label_order = []

    x, y, *yerr = _extract_rl_data(combined_paths['dps'], spread_measure)
    label = 'RL + Interface'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='--')
    label_order.append(label)

    cnn_all_data = _extract_cnn_data(combined_paths['cnn'], cnn_n_train, spread_measure)

    for k, v in cnn_all_data.items():
        x, y, *yerr = v
        label = "CNN - {} hidden units".format(k)
        label_order.append(label)
        ax.errorbar(x, y, yerr=yerr, label=label)

    legend_handles = {l: h for h, l in zip(*ax.get_legend_handles_labels())}
    ordered_handles = [legend_handles[l] for l in label_order]

    legend = ax.legend(ordered_handles, label_order, loc='best', ncol=1, fontsize=8)
    plt.subplots_adjust(left=0.16, bottom=0.15, right=0.97, top=0.96)
    plt.savefig(os.path.join(plot_dir, 'sample_efficiency_combined.pdf'))
    return legend


def gen_super_sample_efficiency():
    fig, _ = plt.subplots(2, 4, sharex=True, sharey=True, figsize=(10.7, 5.5))

    fig.text(0.52, 0.01, '# Training Examples', ha='center', fontsize=12)
    fig.text(0.01, 0.51, '% Test Error', va='center', rotation='vertical', fontsize=12)

    shape = (2, 4)
    indi_axes = [
        plt.subplot2grid(shape, (0, 0)),
        plt.subplot2grid(shape, (0, 1)),
        plt.subplot2grid(shape, (1, 0)),
        plt.subplot2grid(shape, (1, 1))
    ]
    combined_ax = plt.subplot2grid(shape, (0, 2), colspan=2, rowspan=2)

    pp = [
        dict(title='Sum', key='sum'),
        dict(title='Product', key='prod'),
        dict(title='Maximum', key='max'),
        dict(title='Minimum', key='min'),
    ]
    cnn_n_train = [32, 128, 512]
    spread_measure = 'std_err'

    for i, (ax, p) in enumerate(zip(indi_axes, pp)):
        x, y, *yerr = _extract_rl_data(single_op_paths[p['key']]['dps'], spread_measure)
        label = 'RL + Interface'
        ax.errorbar(x, y, yerr=yerr, label=label, ls='--')

        cnn_sum_data = _extract_cnn_data(single_op_paths[p['key']]['cnn'], cnn_n_train, spread_measure)

        for k, v in cnn_sum_data.items():
            x, y, *yerr = v
            label = "CNN - {} hidden units".format(k)
            ax.errorbar(x, y, yerr=yerr, label=label)

        ax.set_title(p['title'])
        ax.tick_params(axis='both', labelsize=14)
        ax.set_ylim((0.0, 100.0))
        ax.set_xscale('log', basex=2)

    # Combined
    combined_ax.set_title("Combined Task")
    combined_ax.tick_params(axis='both', labelsize=14)
    combined_ax.set_ylim((0.0, 100.0))
    combined_ax.set_xscale('log', basex=2)

    label_order = []

    x, y, *yerr = _extract_rl_data(combined_paths['dps'], spread_measure)
    label = 'RL + Interface'
    combined_ax.errorbar(x, y, yerr=yerr, label=label, ls='--')
    label_order.append(label)

    cnn_all_data = _extract_cnn_data(combined_paths['cnn'], cnn_n_train, spread_measure)

    for k, v in cnn_all_data.items():
        x, y, *yerr = v
        label = "CNN - {} hidden units".format(k)
        label_order.append(label)
        combined_ax.errorbar(x, y, yerr=yerr, label=label)

    legend_handles = {l: h for h, l in zip(*combined_ax.get_legend_handles_labels())}
    ordered_handles = [legend_handles[l] for l in label_order]

    combined_ax.legend(ordered_handles, label_order, loc='best', ncol=1)

    plt.subplots_adjust(
        left=0.09, bottom=0.11, right=0.97, top=0.95, wspace=0.13, hspace=0.20)

    plt.savefig(os.path.join(plot_dir, 'sample_efficiency_super.pdf'))


def y_func(r):
    return 100 * r['test_01_loss']


def gen_size_curriculum():
    fig, axes = plt.subplots(1, 3, sharex=True, sharey=True, figsize=(10, 5))
    spread_measure = 'std_err'
    fig.text(0.52, 0.01, '# Training Examples on Test Task', ha='center', fontsize=12)

    # ********************************************************************************

    ax = axes[0]
    ax.set_title('3x3, 2-3 digits')
    ax.set_ylabel('% Test Error', fontsize=12)
    ax.tick_params(axis='both', labelsize=14)
    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    label_order = []

    # x, y, *yerr = _extract_rl_data(size_paths['A']['dps'], spread_measure)
    # label = 'RL + Interface - With Curric'
    # line, _, _ = ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    # label_order.append(label)

    # rl_colour = line.get_c()

    # x, y, *yerr = _extract_rl_data(size_paths['D']['dps'], spread_measure)
    # label = 'RL + Interface - No Curric'
    # ax.errorbar(x, y, yerr=yerr, label=label, ls='--', c=rl_colour)
    # label_order.append(label)

    x, y, *yerr = _extract_cnn_data(size_paths['A']['cnn'], 512, spread_measure, y_func, 'curriculum:-1:n_train')
    label = 'CNN - With Curric'
    line, _, _ = ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    label_order.append(label)

    cnn_colour = line.get_c()

    x, y, *yerr = _extract_cnn_data(size_paths['D']['cnn'], 512, spread_measure, y_func, 'curriculum:-1:n_train')
    label = 'CNN - No Curric'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='--', c=cnn_colour)
    label_order.append(label)

    legend_handles = {l: h for h, l in zip(*ax.get_legend_handles_labels())}
    ordered_handles = [legend_handles[l] for l in label_order]

    ax.legend(ordered_handles, label_order, loc='best', ncol=1)

    # ********************************************************************************

    ax = axes[1]
    ax.set_title('3x3, 4 digits')
    ax.tick_params(axis='both', labelsize=14)
    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    # x, y, *yerr = _extract_rl_data(size_paths['B']['dps'], spread_measure,)
    # ax.errorbar(x, y, yerr=yerr, ls='-', c=rl_colour)

    # x, y, *yerr = _extract_rl_data(size_paths['E']['dps'], spread_measure,)
    # ax.errorbar(x, y, yerr=yerr, ls='--', c=rl_colour)

    x, y, *yerr = _extract_cnn_data(size_paths['B']['cnn'], 512, spread_measure, y_func, 'curriculum:-1:n_train')
    ax.errorbar(x, y, yerr=yerr, ls='-', c=cnn_colour)

    x, y, *yerr = _extract_cnn_data(size_paths['E']['cnn'], 512, spread_measure, y_func, 'curriculum:-1:n_train')
    ax.errorbar(x, y, yerr=yerr, ls='--', c=cnn_colour)

    # ********************************************************************************

    ax = axes[2]
    ax.set_title('3x3, 5 digits')
    ax.tick_params(axis='both', labelsize=14)
    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    # x, y, *yerr = _extract_rl_data(size_paths['B']['dps'], spread_measure,)
    # ax.errorbar(x, y, yerr=yerr, ls='-', c=rl_colour)

    # x, y, *yerr = _extract_rl_data(size_paths['E']['dps'], spread_measure,)
    # ax.errorbar(x, y, yerr=yerr, ls='--', c=rl_colour)

    x, y, *yerr = _extract_cnn_data(size_paths['C']['cnn'], 512, spread_measure, y_func, 'curriculum:-1:n_train')
    ax.errorbar(x, y, yerr=yerr, ls='-', c=cnn_colour)

    x, y, *yerr = _extract_cnn_data(size_paths['F']['cnn'], 512, spread_measure, y_func, 'curriculum:-1:n_train')
    ax.errorbar(x, y, yerr=yerr, ls='--', c=cnn_colour)

    plt.subplots_adjust(
        left=0.09, bottom=0.11, right=0.97, top=0.95, wspace=0.13, hspace=0.20)

    plt.savefig(os.path.join(plot_dir, 'size_curriculum.pdf'))


def gen_parity_curriculum():
    fig, ax = plt.subplots(1, 1, sharex=True, sharey=True, figsize=(10, 5))
    spread_measure = 'std_err'
    fig.text(0.52, 0.01, '# Training Examples on Test Task', ha='center', fontsize=12)

    ax.set_title('')
    ax.set_ylabel('% Test Error', fontsize=12)
    ax.tick_params(axis='both', labelsize=14)
    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    label_order = []

    x, y, *yerr = _extract_rl_data(parity_paths['B']['dps'], spread_measure, lambda r: -100 * r['test_reward_per_ep'])
    label = 'RL + Interface - With Curric'
    line, _, _ = ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    label_order.append(label)

    # records = extract_verbose_data_from_job(parity_paths['B']['dps'], 'n_train')
    # initial_error = [-100.0 * r['test_data'].loc[0]['reward_per_ep'] for r in records]
    # y = np.mean(initial_error)
    # l = ax.axhline(y)
    # fill_kwargs = {'facecolor': k, 'edgecolor': k, 'alpha': 0.3, 'label': 'Initial Error'}
    # ax.fill_between(X, y_lower, y_upper, **fill_kwargs)

    rl_colour = line.get_c()

    x, y, *yerr = _extract_rl_data(parity_paths['C']['dps'], spread_measure, lambda r: -100 * r['test_reward_per_ep'])
    label = 'RL + Interface - No Curric'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='--', c=rl_colour)
    label_order.append(label)

    def y_func(r):
        return -100 * r['test_reward']

    x, y, *yerr = _extract_cnn_data(parity_paths['B']['cnn'], 512, spread_measure, y_func, 'curriculum:-1:n_train')
    label = 'CNN - With Curric'
    line, _, _ = ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    label_order.append(label)

    cnn_colour = line.get_c()

    x, y, *yerr = _extract_cnn_data(parity_paths['C']['cnn'], 512, spread_measure, y_func, 'curriculum:-1:n_train')
    label = 'CNN - No Curric'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='--', c=cnn_colour)
    label_order.append(label)

    legend_handles = {l: h for h, l in zip(*ax.get_legend_handles_labels())}
    ordered_handles = [legend_handles[l] for l in label_order]

    ax.legend(ordered_handles, label_order, loc='best', ncol=1)

    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)
    plt.subplots_adjust(
        left=0.09, bottom=0.11, right=0.97, top=0.95, wspace=0.13, hspace=0.20)

    plt.savefig(os.path.join(plot_dir, 'parity_curriculum.pdf'))


def gen_curric():
    fig, axes = plt.subplots(1, 3, sharex=True, sharey=True, figsize=(10, 3.7))
    spread_measure = 'std_err'
    fig.text(0.52, 0.01, '# Training Examples on Test Task', ha='center', fontsize=12)

    ax = axes[0]

    ax.set_title('3x3, 2-3 digits')
    ax.set_ylabel('% Test Error', fontsize=12)
    ax.tick_params(axis='both', labelsize=14)
    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    label_order = []

    x, y, *yerr = _extract_rl_data(size_paths['A']['dps'], spread_measure)
    label = 'RL + Interface - With Curric'
    line, _, _ = ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    label_order.append(label)

    rl_colour = line.get_c()

    x, y, *yerr = _extract_rl_data(size_paths['C']['dps'], spread_measure)
    label = 'RL + Interface - No Curric'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='--', c=rl_colour)
    label_order.append(label)

    x, y, *yerr = _extract_cnn_data(size_paths['A']['cnn'], 512, spread_measure, 'curriculum:-1:n_train')
    label = 'CNN - With Curric'
    line, _, _ = ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    label_order.append(label)

    cnn_colour = line.get_c()

    x, y, *yerr = _extract_cnn_data(size_paths['C']['cnn'], 512, spread_measure, 'curriculum:-1:n_train')
    label = 'CNN - No Curric'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='--', c=cnn_colour)
    label_order.append(label)

    ax = axes[1]
    ax.set_title('3x3, 4 digits')
    ax.tick_params(axis='both', labelsize=14)
    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    x, y, *yerr = _extract_rl_data(size_paths['B']['dps'], spread_measure)
    ax.errorbar(x, y, yerr=yerr, ls='-', c=rl_colour)

    x, y, *yerr = _extract_rl_data(size_paths['F']['dps'], spread_measure)
    ax.errorbar(x, y, yerr=yerr, ls='--', c=rl_colour)

    x, y, *yerr = _extract_cnn_data(size_paths['B']['cnn'], 512, spread_measure, 'curriculum:-1:n_train')
    ax.errorbar(x, y, yerr=yerr, ls='-', c=cnn_colour)

    x, y, *yerr = _extract_cnn_data(size_paths['F']['cnn'], 512, spread_measure, 'curriculum:-1:n_train')
    ax.errorbar(x, y, yerr=yerr, ls='--', c=cnn_colour)

    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    ax = axes[2]
    ax.set_title('even -> odd')
    ax.tick_params(axis='both', labelsize=14)
    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    label_order = []

    x, y, *yerr = _extract_rl_data(parity_paths['A']['dps'], spread_measure)
    label = 'RL + Interface - With Curric'
    line, _, _ = ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    label_order.append(label)

    rl_colour = line.get_c()

    x, y, *yerr = _extract_rl_data(parity_paths['C']['dps'], spread_measure)
    label = 'RL + Interface - No Curric'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='--', c=rl_colour)
    label_order.append(label)

    x, y, *yerr = _extract_cnn_data(parity_paths['A']['cnn'], 512, spread_measure, 'curriculum:-1:n_train')
    label = 'CNN - With Curric'
    line, _, _ = ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    label_order.append(label)

    cnn_colour = line.get_c()

    x, y, *yerr = _extract_cnn_data(parity_paths['C']['cnn'], 512, spread_measure, 'curriculum:-1:n_train')
    label = 'CNN - No Curric'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='--', c=cnn_colour)
    label_order.append(label)

    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    legend_handles = {l: h for h, l in zip(*ax.get_legend_handles_labels())}
    ordered_handles = [legend_handles[l] for l in label_order]

    ax.legend(ordered_handles, label_order, loc='best', ncol=1, fontsize=8)

    plt.subplots_adjust(
        left=0.08, bottom=0.15, right=0.98, top=0.91, wspace=0.13, hspace=0.20)

    plt.savefig(os.path.join(plot_dir, 'curriculum.pdf'))


def gen_ablations():
    plt.figure(figsize=(5, 3.5))

    ax = plt.gca()

    ax.set_ylabel('% Test Error', fontsize=12)
    ax.set_xlabel('# Training Examples', fontsize=12)
    ax.tick_params(axis='both', labelsize=14)
    ax.set_ylim((0.0, 100.0))
    ax.set_xscale('log', basex=2)

    spread_measure = 'std_err'

    label_order = []

    x, y, *yerr = _extract_rl_data(ablation_paths['full_interface'], spread_measure, lambda r: 100 * r['test_loss'])
    label = 'Full Interface'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    label_order.append(label)

    x, y, *yerr = _extract_rl_data(ablation_paths['no_modules'], spread_measure, lambda r: 100 * r['test_01_loss'])
    label = 'No Modules'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    label_order.append(label)

    x, y, *yerr = _extract_rl_data(ablation_paths['no_transformations'], spread_measure, lambda r: 100 * r['test_01_loss'])
    label = 'No Transformations'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    label_order.append(label)

    x, y, *yerr = _extract_rl_data(ablation_paths['no_classifiers'], spread_measure, lambda r: 100 * r['test_01_loss'])
    label = 'No Classifiers'
    ax.errorbar(x, y, yerr=yerr, label=label, ls='-')
    label_order.append(label)

    legend_handles = {l: h for h, l in zip(*ax.get_legend_handles_labels())}
    ordered_handles = [legend_handles[l] for l in label_order]

    legend = ax.legend(ordered_handles, label_order, loc='best', ncol=1, fontsize=8)
    plt.subplots_adjust(left=0.16, bottom=0.15, right=0.97, top=0.96)
    plt.savefig(os.path.join(plot_dir, 'ablations.pdf'))
    return legend


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("plots", nargs='+')
    parser.add_argument("--style", default="bmh")
    parser.add_argument("--no-block", action="store_true")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    plt.rc('lines', linewidth=1)

    color_cycle = ['tab:brown', 'tab:orange', 'tab:green', 'tab:cyan']
    os.makedirs(plot_dir, exist_ok=True)

    with plt.style.context(args.style):
        plt.rc('axes', prop_cycle=(cycler('color', color_cycle)))

        if "single_op" in args.plots:
            gen_sample_efficiency_single_op()
        if "combined" in args.plots:
            gen_sample_efficiency_combined()
        if "super" in args.plots:
            gen_super_sample_efficiency()
        if "size" in args.plots:
            gen_size_curriculum()
        if "parity" in args.plots:
            gen_parity_curriculum()
        if "curriculum" in args.plots:
            gen_curric()
        if "ablations" in args.plots:
            gen_ablations()

    if args.show:
        plt.show(block=not args.no_block)
