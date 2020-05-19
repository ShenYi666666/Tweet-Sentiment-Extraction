import argparse
import json
import os
import random
import re
import shutil
from collections import OrderedDict, defaultdict
from functools import partial
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd
import torch
import tqdm
from apex import amp

from sklearn.model_selection import GroupKFold
from torch import nn
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
from transformers import RobertaConfig, RobertaModel, RobertaTokenizer, AutoConfig, AutoModel, AutoTokenizer
from transformers.optimization import (AdamW, get_cosine_schedule_with_warmup,
                                       get_linear_schedule_with_warmup,
                                       get_cosine_with_hard_restarts_schedule_with_warmup)

from utilsv4 import (binary_focal_loss, get_learning_rate, jaccard_list, get_best_pred, ensemble, ensemble_words, prepare,
                   load_model, save_model, set_seed, write_event, evaluate, get_predicts_from_token_logits, map_to_word)


class FGM():
    def __init__(self, model):
        self.model = model
        self.backup = {}

    def attack(self, epsilon=1., emb_name='word_embeddings'):
        for name, param in self.model.named_parameters():
            if param.requires_grad and emb_name in name:
                self.backup[name] = param.data.clone()
                norm = torch.norm(param.grad)
                if norm != 0:
                    r_at = epsilon * param.grad / norm
                    param.data.add_(r_at)

    def restore(self, emb_name='word_embeddings'):
        for name, param in self.model.named_parameters():
            if param.requires_grad and emb_name in name:
                assert name in self.backup
                param.data = self.backup[name]
            self.backup = {}
            
class TrainDataset(Dataset):

    def __init__(self, data, tokenizer, mode='train', smooth=False, epsilon=0.15):
        super(TrainDataset, self).__init__()
        self._tokens = data['tokens'].tolist()
        self._sentilabel = data['senti_label'].tolist()
        self._sentiment = data['sentiment'].tolist()
        if 'type' in data.columns.tolist():
            self._type = data['type'].tolist()
        else:
            self._type = ['normal']*len(self._tokens)
        self._data = data
        self._mode = mode
        self._smooth = smooth
        self._epsilon = epsilon
        if mode in ['train', 'valid']:
            self._start = data['start'].tolist()
            self._end = data['end'].tolist()
            self._all_sentence = data['all_sentence'].tolist()
            self._inst = data['in_st'].tolist()
        else:
            pass
        self._tokenizer = tokenizer
        self._offset = 4 if isinstance(tokenizer, RobertaTokenizer) else 3

    def __len__(self):
        return len(self._tokens)

    def get_other_sample(self, idx, sentiment):
        while True:
            idx = random.randint(0,len(self._tokens)-1)
            if self._sentiment[idx]!=sentiment:
                return self._tokens[idx]

    def __getitem__(self, idx):
        sentiment = self._sentiment[idx]
        tokens = self._tokens[idx]
        if self._mode=='train':
            inst = self._inst[idx]
            whole_sentence = self._all_sentence[idx]

        # if self._mode=='train':
        #     if sentiment!='neutral' and random.random()<-0.05:
        #         aug_tokens = self.get_other_sample(idx, sentiment)
        #         tokens = tokens+aug_tokens
        #         inst = inst+[-100 for i in range(len(aug_tokens))]
        #         whole_sentence = 0

        inputs = self._tokenizer.encode_plus(
            sentiment, tokens, return_tensors='pt')

        token_id = inputs['input_ids'][0]
        if 'token_type_ids' in inputs:
            type_id = inputs['token_type_ids'][0]
        else:
            type_id = torch.zeros_like(token_id)
        mask = inputs['attention_mask'][0]

        if self._mode == 'train':
            inst = [-100]*self._offset+inst+[-100]
            start = self._start[idx]+self._offset
            end = self._end[idx]+self._offset

            if self._smooth:
                start_idx, end_idx = start, end
                start, end = torch.zeros_like(token_id, dtype=torch.float), torch.zeros_like(token_id, dtype=torch.float)
                # start = start*self._epsilon/torch.sum(start)
                # end = end*self._epsilon/torch.sum(end)
                # if self._type[idx]!='normal':
                if True:
                    start[start_idx] += 1-self._epsilon
                    end[end_idx] += 1-self._epsilon

                    # if start_idx>0:
                    #     start[start_idx-1]= self._epsilon/2
                    # if start_idx<len(start)-1:
                    #     start[start_idx+1] = self._epsilon/2
                    # if end_idx>0:
                    #     end[end_idx-1] = self._epsilon/2
                    # if end_idx<len(end)-1:
                    #     end[end_idx+1] = self._epsilon/2
                    start+=self._epsilon/len(mask)
                    end+=self._epsilon/len(mask)
                else:
                    start[start_idx] += 1
                    end[end_idx] += 1
            all_sentence = whole_sentence
        else:
            start, end = 0, 0
            inst = [-100]*len(token_id)
            all_sentence = 0
        return token_id, type_id, mask, self._sentilabel[idx], start, end, torch.LongTensor(inst), all_sentence

class MyCollator:

    def __init__(self, token_pad_value=1, type_pad_value=0):
        super().__init__()
        self.token_pad_value = token_pad_value
        self.type_pad_value = type_pad_value

    def __call__(self, batch):
        tokens, type_ids, masks, label, start, end, inst, all_sentence = zip(*batch)
        tokens = pad_sequence(tokens, batch_first=True, padding_value=self.token_pad_value)
        type_ids = pad_sequence(type_ids, batch_first=True, padding_value=self.type_pad_value)
        masks = pad_sequence(masks, batch_first=True, padding_value=0)
        label = torch.LongTensor(label)
        if not isinstance(start[0], int):
            start = pad_sequence(start, batch_first=True, padding_value=0)
            end = pad_sequence(end, batch_first=True, padding_value=0)
        else:
            start = torch.LongTensor(start)
            end = torch.LongTensor(end)
        all_sentence = torch.FloatTensor(all_sentence)
        inst = pad_sequence(inst, batch_first=True, padding_value=-100)
        return tokens, type_ids, masks, label, start, end, inst, all_sentence


class TweetModel(nn.Module):

    def __init__(self, pretrain_path=None, dropout=0.2, config=None):
        super(TweetModel, self).__init__()
        if config is not None:
            self.bert = AutoModel.from_config(config)
        else:
            self.bert = AutoModel.from_pretrained(
                pretrain_path, cache_dir=None)
        self.head = nn.Sequential(
            OrderedDict([
                ('clf', nn.Linear(self.bert.config.hidden_size, 1))
            ])
        )
        self.cnn = nn.Sequential(
            OrderedDict([
                ('drop1', nn.Dropout(0.1)),
                ('cnn', nn.Conv1d(self.bert.config.hidden_size, self.bert.config.hidden_size, 3, padding=1)),
                ('act', nn.GELU()),
            ])
        )
        self.ext_head = nn.Sequential(
            OrderedDict([
                ('se', nn.Linear(self.bert.config.hidden_size, 2))
            ])
        )
        self.inst_head = nn.Linear(self.bert.config.hidden_size, 2)
        self.dropout = nn.Dropout(0.1)
        # for name, param in self.bert.embeddings.named_parameters():
        #     param.requires_grad = False
        # for l in range(3):
        #     for param in self.bert.encoder.layer[l].parameters():
        #         param.requires_grad = False

    def forward(self, inputs, masks, token_type_ids=None, input_emb=None):
        seq_output, pooled_output = self.bert(
            inputs, masks, token_type_ids=token_type_ids, inputs_embeds=input_emb)

        out = self.head(self.dropout(pooled_output))
        seq_output = self.cnn(seq_output.permute(0,2,1)).permute(0,2,1)
        se_out = self.ext_head(self.dropout(seq_output))  #()
        inst_out = self.inst_head(self.dropout(seq_output))
        return out, se_out[:, :, 0], se_out[:, :, 1], inst_out


def main():
    parser = argparse.ArgumentParser()
    arg = parser.add_argument
    arg('mode', choices=['train', 'validate', 'predict', 'predict5',
                         'validate5', 'validate52'])
    arg('run_root')
    arg('--batch-size', type=int, default=16)
    arg('--step', type=int, default=1)
    arg('--workers', type=int, default=2)
    arg('--lr', type=float, default=0.00002)
    arg('--clean', action='store_true')
    arg('--n-epochs', type=int, default=3)
    arg('--limit', type=int)
    arg('--fold', type=int, default=0)
    arg('--multi-gpu', type=int, default=0)

    arg('--bert-path', type=str, default='../../bert_models/roberta_base/')
    arg('--train-file', type=str, default='train_roberta.pkl')
    arg('--local-test', type=str, default='localtest_roberta.pkl')
    arg('--test-file', type=str, default='test.csv')
    arg('--output-file', type=str, default='result.csv')
    arg('--no-neutral', action='store_true')
    arg('--holdout', action='store_true')

    arg('--epsilon', type=float, default=0.3)
    arg('--max-len', type=int, default=200)
    arg('--fp16', action='store_true')
    arg('--lr-layerdecay', type=float, default=1.0)
    arg('--max_grad_norm', type=float, default=-1.0)
    arg('--weight_decay', type=float, default=0.0)
    arg('--adam-epsilon', type=float, default=1e-6)
    arg('--offset', type=int, default=4)
    arg('--best-loss', action='store_true')
    arg('--abandon', action='store_true')
    arg('--post', action='store_true')
    arg('--smooth', action='store_true')
    arg('--temperature', type=float, default=1.0)

    args = parser.parse_args()
    args.vocab_path = args.bert_path
    set_seed(42)

    run_root = Path('../experiments/' + args.run_root)
    DATA_ROOT = Path('../input/')
    tokenizer = AutoTokenizer.from_pretrained(args.vocab_path, cache_dir=None, do_lower_case=True)
    args.tokenizer = tokenizer
    if args.bert_path.find('roberta'):
        collator = MyCollator()
    else:
        # this is for bert models
        collator = MyCollator(token_pad_value=0, type_pad_value=1)
    folds = pd.read_pickle(DATA_ROOT / args.train_file)
    if args.mode in ['train', 'validate', 'validate5', 'validate55', 'teacherpred']:
        # folds = pd.read_pickle(DATA_ROOT / args.train_file)
        train_fold = folds[folds['fold'] != args.fold]
        if args.abandon:
            train_fold = train_fold[train_fold['label_jaccard']>0.6]
        if args.no_neutral:
            train_fold = train_fold[train_fold['sentiment']!='neutral']
        valid_fold = folds[folds['fold'] == args.fold]
        # remove pseudo samples
        if 'type' in valid_fold.columns.tolist():
            valid_fold = valid_fold[valid_fold['type']=='normal']
        print(valid_fold.shape)
        print('training fold:', args.fold)
        if args.limit:
            train_fold = train_fold[:args.limit]
            valid_fold = valid_fold[:args.limit]

    if args.mode == 'train':
        if run_root.exists() and args.clean:
            shutil.rmtree(run_root)
        run_root.mkdir(exist_ok=True, parents=True)

        training_set = TrainDataset(train_fold, tokenizer=tokenizer, smooth=args.smooth)
        training_loader = DataLoader(training_set, collate_fn=collator,
                                     shuffle=True, batch_size=args.batch_size,
                                     num_workers=args.workers)

        valid_set = TrainDataset(valid_fold, tokenizer=tokenizer, mode='test')
        valid_loader = DataLoader(valid_set, batch_size=args.batch_size, shuffle=False, collate_fn=collator,
                                  num_workers=args.workers)

        model = TweetModel(args.bert_path)
        model.cuda()

        config = RobertaConfig.from_pretrained(args.bert_path)
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay) and p.requires_grad],
                "weight_decay": args.weight_decay,
            },
            {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay) and p.requires_grad], "weight_decay": 0.0},
        ]
        optimizer = AdamW(optimizer_grouped_parameters,
                          lr=args.lr, eps=args.adam_epsilon)
        if args.fp16:
            model, optimizer = amp.initialize(
                model, optimizer, opt_level="O2", verbosity=0)

        total_steps = int(len(train_fold) * args.n_epochs / args.step / args.batch_size)
        warmup_steps = int(0.1 * total_steps)
        scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=warmup_steps,
                                                    num_training_steps=total_steps)
        
        if args.multi_gpu == 1:
            model = nn.DataParallel(model)

        train(args, model, optimizer, scheduler,
              train_loader=training_loader,
              valid_df=valid_fold,
              valid_loader=valid_loader, epoch_length=len(training_loader)*args.batch_size)

    elif args.mode == 'validate5':
        valid_fold = pd.read_pickle(DATA_ROOT / args.local_test)
        config = AutoConfig.from_pretrained(args.bert_path)
        model = TweetModel(config=config)
        all_start_preds, all_end_preds = [], []
        for fold in range(5):
            load_model(model, run_root / ('best-model-%d.pt' % fold))
            model.cuda()
            valid_set = TrainDataset(valid_fold, tokenizer=tokenizer, mode='test')
            valid_loader = DataLoader(valid_set, batch_size=args.batch_size, shuffle=False, collate_fn=collator,
                                      num_workers=args.workers)
            all_whole_preds, fold_start_pred, fold_end_pred, fold_inst_preds = predict(
                model, valid_fold, valid_loader, args, progress=True)
            all_start_preds.append(fold_start_pred)
            all_end_preds.append(fold_end_pred)
        all_start_preds, all_end_preds = ensemble(None, all_start_preds, all_end_preds, valid_fold)
        word_preds, scores = get_predicts_from_token_logits(all_whole_preds, all_start_preds, all_end_preds, valid_fold, args)
        metrics = evaluate(word_preds, valid_fold, args)
    
    elif args.mode == 'validate52':
        # 在答案层面融合
        valid_fold = pd.read_pickle(DATA_ROOT / args.local_test)
        config = AutoConfig.from_pretrained(args.bert_path)
        model = TweetModel(config=config)
        all_word_preds = []
        for fold in range(5):
            load_model(model, run_root / ('best-model-%d.pt' % fold))
            model.cuda()
            valid_set = TrainDataset(valid_fold, tokenizer=tokenizer, mode='test')
            valid_loader = DataLoader(valid_set, batch_size=args.batch_size, shuffle=False, collate_fn=collator,
                                      num_workers=args.workers)
            all_senti_preds, fold_start_pred, fold_end_pred, fold_inst_preds = predict(
                model, valid_fold, valid_loader, args, progress=True)
            fold_word_preds, scores = get_predicts_from_token_logits(fold_start_pred, fold_end_pred, valid_fold, args)
            all_word_preds.append(fold_word_preds)

        word_preds = ensemble_words(all_word_preds)
        metrics = evaluate(word_preds, valid_fold, args)

    elif args.mode == 'validate':
        if args.holdout:
            valid_fold = pd.read_pickle(DATA_ROOT / args.local_test)
        valid_set = TrainDataset(valid_fold, tokenizer=tokenizer, mode='test')
        valid_loader = DataLoader(valid_set, batch_size=args.batch_size, shuffle=False, collate_fn=collator,
                                  num_workers=args.workers)
        valid_result = valid_fold.copy()
        config = AutoConfig.from_pretrained(args.bert_path)
        model = TweetModel(config=config)
        load_model(model, run_root / ('best-model-%d.pt' % args.fold))
        model.cuda()
        if args.multi_gpu == 1:
            model = nn.DataParallel(model)

        all_whole_preds, all_start_preds, all_end_preds, all_inst_preds = predict(
            model, valid_fold, valid_loader, args, progress=True)
        word_preds, inst_word_preds, scores = get_predicts_from_token_logits(all_whole_preds, all_start_preds, all_end_preds, all_inst_preds, valid_fold, args)
        metrics = evaluate(word_preds, valid_fold, args)
        # metrics = evaluate(inst_word_preds, valid_fold, args)
        valid_fold['pred'] = word_preds
        # valid_fold['inst_pred'] = inst_word_preds
        valid_fold.to_csv(run_root/('pred-%d.csv'%args.fold), sep='\t', index=False)

    elif args.mode in ['predict', 'predict5']:
        test = pd.read_csv(DATA_ROOT / 'tweet-sentiment-extraction'/args.test_file)
        test.dropna(subset=['text','selected_text'], inplace=True)
        test['text'] = test['text'].apply(lambda x: ' '.join(x.lower().strip().split()))
        test['fold'] = folds['fold'].values
        test = test[test['fold']==args.fold]
        if args.limit:
            test = test.iloc[:args.limit]
        
        data = []
        for text in test['text'].tolist():
            tokens, invert_map = [], []

            words, first_char, _ = prepare(text)

            for idx, w in enumerate(words):
                w = w.replace("`","'")
                if first_char[idx]:
                    prefix = " "
                else:
                    prefix = ""
                for token in tokenizer.tokenize(prefix+w):
                    tokens.append(token)
                    invert_map.append(idx)

            data.append((words, first_char, tokens, invert_map))
        words, first_char, tokens, invert_map = zip(*data)
        test['words'] = words
        test['tokens'] = tokens
        test['invert_map'] = invert_map
        test['first_char'] = first_char
        senti2label = {
            'positive':2,
            'negative':0,
            'neutral':1
        }
        test['senti_label']=test['sentiment'].apply(lambda x: senti2label[x])

        test_set = TrainDataset(test, tokenizer=tokenizer, mode='test')
        test_loader = DataLoader(test_set, batch_size=args.batch_size, shuffle=False, collate_fn=collator,
                                 num_workers=args.workers)
        config = AutoConfig.from_pretrained(args.bert_path)
        model = TweetModel(config=config)
        
        if args.mode == 'predict':
            load_model(model, run_root / ('best-model-%d.pt' % args.fold))
            class Args:
                post = True
                tokenizer = RobertaTokenizer.from_pretrained(args.bert_path)
                offset = 4
                batch_size = 16
                workers = 1
                bert_path = args.bert_path
            args = Args()
            model.cuda()

            all_whole_preds, all_start_preds, all_end_preds, all_inst_preds = predict(
                model, test, test_loader, args, progress=True)
            word_preds, inst_word_preds, scores = get_predicts_from_token_logits(all_whole_preds, all_start_preds, all_end_preds, all_inst_preds, test, args)
            metrics = evaluate(word_preds, test, args)
        
        if args.mode == 'predict5':
            all_whole_preds, all_start_preds, all_end_preds, all_inst_preds = [], [], [], []
            for fold in range(2):
                load_model(model, run_root / ('best-model-%d.pt' % fold))
                model.cuda()
                fold_whole_preds, fold_start_preds, fold_end_preds, fold_inst_preds = predict(model, test, test_loader, args, progress=True, for_ensemble=True)
                # fold_start_preds = map_to_word(fold_start_preds, test, args, softmax=False)
                # fold_end_preds = map_to_word(fold_end_preds, test, args, softmax=False)
                # fold_inst_preds = map_to_word(fold_inst_preds, test, args, softmax=False)

                all_whole_preds.append(fold_whole_preds)
                all_start_preds.append(fold_start_preds)
                all_end_preds.append(fold_end_preds)
                all_inst_preds.append(fold_inst_preds)

            all_whole_preds, all_start_preds, all_end_preds, all_inst_preds = ensemble(all_whole_preds, all_start_preds, all_end_preds, all_inst_preds, test)
            word_preds, inst_word_preds, scores = get_predicts_from_token_logits(all_whole_preds, all_start_preds, all_end_preds, all_inst_preds, test, args)

        test['selected_text'] = word_preds
        test[['textID','selected_text']].to_csv('submission.csv', index=False)

def train(args, model: nn.Module, optimizer, scheduler, *,
          train_loader, valid_df, valid_loader, epoch_length, n_epochs=None) -> bool:
    n_epochs = n_epochs or args.n_epochs

    run_root = Path('../experiments/' + args.run_root)
    model_path = run_root / ('model-%d.pt' % args.fold)
    best_model_path = run_root / ('best-model-%d.pt' % args.fold)
    best_model_loss_path = run_root / ('best-loss-%d.pt' % args.fold)
    
    best_valid_score = 0
    best_valid_loss = 1e10
    start_epoch = 0
    best_epoch = 0
    step = 0
    log = run_root.joinpath('train-%d.log' %
                            args.fold).open('at', encoding='utf8')

    update_progress_steps = int(epoch_length / args.batch_size / 100)
    kl_fn = nn.KLDivLoss(reduction='batchmean')
    loss_fn = nn.CrossEntropyLoss(ignore_index=-100)
    bce_fn = nn.BCEWithLogitsLoss()
    # loss_fn = nn.NLLLoss()
    fgm = FGM(model)
    for epoch in range(start_epoch, start_epoch + n_epochs):
        model.train()
        tq = tqdm.tqdm(total=epoch_length)
        losses = []
        mean_loss = 0
        for i, (tokens, types, masks, targets, starts, ends, inst, all_sentence) in enumerate(train_loader):
            lr = get_learning_rate(optimizer)
            batch_size, length = tokens.size(0), tokens.size(1)
            masks = masks.cuda()
            tokens, targets = tokens.cuda(), targets.cuda()
            types = types.cuda()
            starts, ends, inst = starts.cuda(), ends.cuda(), inst.cuda()
            all_sentence = all_sentence.cuda()

            whole_out, start_out, end_out, inst_out = model(tokens, masks, types)
            # start_out = start_out.masked_fill(~masks.bool(), -10000.0)
            # end_out = end_out.masked_fill(~masks.bool(), -10000.0)
            # 正常loss
            whole_loss = bce_fn(whole_out, all_sentence.view(-1, 1))
            if args.smooth:
                start_out = torch.log_softmax(start_out, dim=-1)
                end_out = torch.log_softmax(end_out, dim=-1)
                start_loss, end_loss = kl_fn(start_out, starts), kl_fn(end_out, ends)
            else:
                start_loss = loss_fn(start_out, starts)
                end_loss = loss_fn(end_out, ends)
            inst_loss = loss_fn(inst_out.permute(0,2,1), inst)
            loss = (start_loss+end_loss)+inst_loss+whole_loss

            loss /= args.step

            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            # fgm.attack() 
            # whole_out, start_out, end_out, inst_out = model(tokens, masks, types)
            # # start_out = start_out.masked_fill(~masks.bool(), -10000.0)
            # # end_out = end_out.masked_fill(~masks.bool(), -10000.0)
            # whole_loss = bce_fn(whole_out, all_sentence.view(-1, 1))
            # if args.smooth:
            #     start_out = torch.log_softmax(start_out, dim=-1)
            #     end_out = torch.log_softmax(end_out, dim=-1)
            #     start_loss, end_loss = kl_fn(start_out, starts), kl_fn(end_out, ends)
            # else:
            #     start_loss = loss_fn(start_out, starts)
            #     end_loss = loss_fn(end_out, ends)
            # inst_loss = loss_fn(inst_out.permute(0,2,1), inst)
            # loss = (start_loss+end_loss)+inst_loss+whole_loss

            # if args.fp16:
            #     with amp.scale_loss(loss, optimizer) as scaled_loss:
            #         scaled_loss.backward()
            # else:
            #     loss.backward()
                
            # fgm.restore()
            if i%args.step==0:
                if args.max_grad_norm > 0:
                    if args.fp16:
                        torch.nn.utils.clip_grad_norm_(
                            amp.master_params(optimizer), args.max_grad_norm)
                    else:
                        torch.nn.utils.clip_grad_norm(model.parameters(), args.max_grad_norm)
                optimizer.step()
                optimizer.zero_grad()
                scheduler.step()

            losses.append(loss.item() * args.step)
            mean_loss = np.mean(losses[-1000:])

            if i > 0 and i % update_progress_steps == 0:
                tq.set_description(f'Epoch {epoch}, lr {lr:.6f}')
                tq.update(update_progress_steps*args.batch_size)
                tq.set_postfix(loss=f'{mean_loss:.5f}')
            step += 1
        tq.close()
        valid_metrics = validation(model, valid_df, valid_loader, args)
        write_event(log, step, epoch=epoch, **valid_metrics)
        current_score = valid_metrics['dirty_score_word']
        save_model(model, str(model_path), current_score, epoch + 1)
        if current_score > best_valid_score:
            best_valid_score = current_score
            shutil.copy(str(model_path), str(best_model_path))
            best_epoch = epoch
            print('model saved')
    os.remove(model_path)
    return True


def predict(model: nn.Module, valid_df, valid_loader, args, progress=False, for_ensemble=False) -> Dict[str, float]:
    # run_root = Path('../experiments/' + args.run_root)
    model.eval()
    all_end_pred, all_whole_pred, all_start_pred, all_inst_out = [], [], [], []
    if progress:
        tq = tqdm.tqdm(total=len(valid_df))
    with torch.no_grad():
        for tokens, types, masks, _, _, _, _, _ in valid_loader:
            if progress:
                batch_size = tokens.size(0)
                tq.update(batch_size)
            masks = masks.cuda()
            tokens = tokens.cuda()
            types = types.cuda()
            whole_out, start_out, end_out, inst_out = model(tokens, masks, types)
            start_out = start_out.masked_fill(~masks.bool(), -1000)
            end_out= end_out.masked_fill(~masks.bool(), -1000)
            all_whole_pred.append(torch.sigmoid(whole_out).cpu().numpy())
            inst_out = torch.softmax(inst_out, dim=-1)
            for idx in range(len(start_out)):
                all_start_pred.append(start_out[idx,:].cpu())
                all_end_pred.append(end_out[idx,:].cpu())
                all_inst_out.append(inst_out[idx,:,1].cpu())
            assert all_start_pred[-1].dim()==1

    all_whole_pred = np.concatenate(all_whole_pred)
    
    if progress:
        tq.close()
    return all_whole_pred, all_start_pred, all_end_pred, all_inst_out


def validation(model: nn.Module, valid_df, valid_loader, args, save_result=False, progress=False):
    run_root = Path('../experiments/' + args.run_root)

    all_whole_preds, all_start_preds, all_end_preds, all_inst_out = predict(
        model, valid_df, valid_loader, args)
    word_preds, inst_preds, scores = get_predicts_from_token_logits(all_whole_preds, all_start_preds, all_end_preds, all_inst_out, valid_df, args)
    # metrics = evaluate(inst_preds, valid_df, args)
    metrics = evaluate(word_preds, valid_df, args)
    return metrics


if __name__ == '__main__':
    main()
