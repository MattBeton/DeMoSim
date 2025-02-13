import argparse
import math
import torch
import wandb
import random
import numpy as np
from tqdm import tqdm

from DistributedSim.models.dataset import *
from DistributedSim.models.nanogpt import *

class GPTTrainDataset(torch.utils.data.Dataset):
    """Simple dataset wrapper for training data"""
    def __init__(self, data, block_size):
        self.data = data
        self.block_size = block_size

    def __len__(self):
        return len(self.data) - self.block_size - 1

    def __getitem__(self, idx):
        x = self.data[idx : idx + self.block_size + 1]
        # return x, x
        return x[:-1], x[1:]

def train_iteration(model, optimizer, scheduler, criterion, batch, device):
    """
    A single training iteration that computes the loss and token accuracy.
    """
    x, y = batch  # x and y have shape [batch_size, block_size]
    x = x.to(device)
    y = y.to(device)
    optimizer.zero_grad()
    logits = model(x)  # expected output shape: [batch_size, block_size, vocab_size]
    loss = criterion(logits.view(-1, logits.size(-1)), y.view(-1))
    loss.backward()
    optimizer.step()
    scheduler.step()

    # Compute prediction accuracy:
    predictions = torch.argmax(logits, dim=-1)  # shape: [batch_size, block_size]
    acc = (predictions == y).float().mean().item()
    return loss.item(), acc

def eval_iteration(model, criterion, val_loader, device, val_size, batch_size):
    """
    Run evaluation for a fixed number of batches and compute the average loss and accuracy.
    """
    model.eval()
    val_loss_total = 0.0
    val_acc_total = 0.0
    val_steps = 0
    val_batches = int(val_size / batch_size)
    with torch.no_grad():
        for batch in val_loader:
            x, y = batch
            x = x.to(device)
            y = y.to(device)
            logits = model(x)
            loss = criterion(logits.view(-1, logits.size(-1)), y.view(-1))
            val_loss_total += loss.item()

            predictions = torch.argmax(logits, dim=-1)
            acc = (predictions == y).float().mean().item()
            val_acc_total += acc

            val_steps += 1
            if val_steps >= val_batches:
                break

    model.train()
    avg_loss = val_loss_total / val_steps if val_steps > 0 else float('inf')
    avg_acc = val_acc_total / val_steps if val_steps > 0 else 0.0
    return avg_loss, avg_acc

def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--block_size", type=int, default=1024, help="Sequence block size")
    parser.add_argument("--batch_size", type=int, default=32, help="Batch size")
    parser.add_argument("--lr", type=float, default=1e-3, help="Initial learning rate")
    parser.add_argument("--warmup_steps", type=int, default=100, help="Number of warmup steps")
    parser.add_argument("--val_interval", type=int, default=100, help="Validation interval")
    parser.add_argument("--val_size", type=int, default=320, help="Number of samples to use for validation")
    parser.add_argument("--max_steps", type=int, default=None, help="Maximum number of training steps")
    parser.add_argument("--dataset", type=str, default="shakespeare", help="which dataset to use (shakespeare, wikitext, code)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--device", type=str, default="cuda", help="Device to use (cuda, cpu, mps)")
    args = parser.parse_args()

    # Set random seeds for reproducibility
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    # Initialize wandb for logging
    wandb.init(project="simple-char-train", config=vars(args))

    # Load dataset (train_data and val_data are 1D tensors of ints)
    # train_data, val_data, vocab_size = get_dataset_small(args)
    train_data, val_data, vocab_size, tokenizer = get_dataset(args, return_tokenizer=True)
    
    # Create datasets and dataloaders
    train_dataset = GPTTrainDataset(train_data, args.block_size)
    val_dataset = GPTTrainDataset(val_data, args.block_size)
    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, 
                                               generator=torch.Generator().manual_seed(args.seed))
    val_loader = torch.utils.data.DataLoader(val_dataset, batch_size=args.batch_size, shuffle=True)

    # Initialize the model
    model = GPT(GPTConfig(
            block_size=args.block_size,
            vocab_size=vocab_size,
            n_layer=12,
            n_head=12,
            n_embd=768,
        ))
    # model = GPT(GPTConfig(vocab_size=vocab_size, 
    #                       block_size=args.block_size, 
    #                       n_layer=2, 
    #                       n_head=2, 
    #                       n_embd=128))
    model.to(args.device)
    model.train()

    # Set up optimizer and a scheduler with linear warmup followed by cosine annealing.
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    total_steps = len(train_loader) if args.max_steps is None else args.max_steps  # Single epoch or max steps
    warmup_steps = args.warmup_steps

    def lr_lambda(current_step):
        if current_step < warmup_steps:
            return float(current_step) / float(max(1, warmup_steps))
        else:
            progress = float(current_step - warmup_steps) / float(max(1, total_steps - warmup_steps))
            return 0.5 * (1.0 + math.cos(math.pi * progress))

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)

    # Define the loss function (cross-entropy for language modeling)
    criterion = torch.nn.CrossEntropyLoss()

    # Training loop with interleaved validation
    global_step = 0
    train_loss_total = 0.0
    train_acc_total = 0.0  # Accumulate training accuracy
    
    pbar = tqdm(total=total_steps, desc="Training")
    train_iter = iter(train_loader)
    
    while global_step < total_steps:
        try:
            batch = next(train_iter)
        except StopIteration:
            train_iter = iter(train_loader)
            batch = next(train_iter)

        # print(batch[0].shape, batch[1].shape)
        # # print(tokenizer.decode(batch[0][0,:], skip_special_tokens=True))
        # print(batch[0][0,:])
        # print('GAPGAPGAPGAPGAPGAP')
        # print(batch[1][0,:])
        # # print(tokenizer.decode(batch[1][0,:], skip_special_tokens=True))
        
            
        # Training step with accuracy computation
        loss, train_acc = train_iteration(model, optimizer, scheduler, criterion, batch, args.device)
        global_step += 1
        train_loss_total += loss
        train_acc_total += train_acc

        current_lr = scheduler.get_last_lr()[0] if hasattr(scheduler, "get_last_lr") else optimizer.param_groups[0]["lr"]
        wandb.log({
            "train_loss": loss,
            "learning_rate": current_lr,
            "train_accuracy": train_acc
        }, step=global_step)
        
        pbar.set_postfix({'loss': f'{loss:.4f}', 'lr': f'{current_lr:.6f}', 'acc': f'{train_acc:.4f}'})
        pbar.update()

        # Validation step every val_interval iterations
        if global_step % args.val_interval == 0:
            avg_train_loss = train_loss_total / args.val_interval
            avg_train_acc = train_acc_total / args.val_interval
            avg_val_loss, avg_val_acc = eval_iteration(model, criterion, val_loader, args.device, args.val_size, args.batch_size)

            # Compute perplexity (only if loss is below a threshold to avoid overflow)
            train_perplexity = math.exp(avg_train_loss) if avg_train_loss < 20 else float('inf')
            val_perplexity = math.exp(avg_val_loss) if avg_val_loss < 20 else float('inf')

            # Log metrics to wandb
            wandb.log({
                "avg_train_loss": avg_train_loss,
                "avg_val_loss": avg_val_loss,
                "train_perplexity": train_perplexity,
                "val_perplexity": val_perplexity,
                "avg_train_accuracy": avg_train_acc,
                "avg_val_accuracy": avg_val_acc,
                "learning_rate": current_lr
            }, step=global_step)
            
            train_loss_total = 0.0  # Reset running loss
            train_acc_total = 0.0   # Reset running accuracy

    pbar.close()
    wandb.finish()

if __name__ == "__main__":
    main()