from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.core.paginator import Paginator
from decimal import Decimal
import csv
import json
from .models import *
from .utils import InterestCalculator, YieldCalculator
from .ai_analytics import MarketIntelligence
from .ai_models import nft_predictor
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt


def home(request):
    """Homepage view"""
    collections = NFTCollection.objects.filter(is_active=True)
    context = {
        'collections': collections,
        'total_users': UserProfile.objects.count(),
        'total_loans': Loan.objects.filter(status='ACTIVE').count(),
    }
    return render(request, 'mosaical_platform/home.html', context)

def register_view(request):
    """User registration"""
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            UserProfile.objects.create(user=user, vbtc_balance=0)
            username = form.cleaned_data.get('username')
            messages.success(request, f'Account created for {username}!')
            return redirect('login')
    else:
        form = UserCreationForm()
    return render(request, 'registration/register.html', {'form': form})

@login_required
def dashboard(request):
    """User dashboard"""
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    user_nfts = NFTVault.objects.filter(owner=request.user).exclude(status='WITHDRAWN')
    user_loans = Loan.objects.filter(borrower=request.user, status='ACTIVE')
    recent_transactions = Transaction.objects.filter(user=request.user).order_by('-created_at')[:10]

    # Calculate totals
    total_nft_value = sum(nft.estimated_value or 0 for nft in user_nfts)
    total_debt = sum(loan.current_debt or 0 for loan in user_loans)

    context = {
        'user_nfts': user_nfts,
        'user_loans': user_loans,
        'profile': profile,
        'recent_transactions': recent_transactions,
        'total_nft_value': total_nft_value,
        'total_debt': total_debt,
    }
    return render(request, 'mosaical_platform/dashboard.html', context)

@login_required
def nft_list(request):
    """List user's NFTs"""
    user_nfts = NFTVault.objects.filter(owner=request.user).exclude(status='WITHDRAWN')
    return render(request, 'mosaical_platform/nft_list.html', {'user_nfts': user_nfts})

@login_required
def deposit_nft(request):
    """Deposit NFT into vault"""
    if request.method == 'POST':
        collection_id = request.POST.get('collection')
        token_id = request.POST.get('token_id')
        name = request.POST.get('name')
        estimated_value = Decimal(request.POST.get('estimated_value', '0'))
        utility_score = int(request.POST.get('utility_score', '50'))

        try:
            collection = NFTCollection.objects.get(id=collection_id)

            # Check if NFT already exists
            if NFTVault.objects.filter(collection=collection, token_id=token_id).exists():
                messages.error(request, 'This NFT already exists in the system!')
                return redirect('deposit_nft')

            # Create NFT vault entry
            nft = NFTVault.objects.create(
                owner=request.user,
                collection=collection,
                token_id=token_id,
                name=name,
                estimated_value=estimated_value,
                utility_score=utility_score,
                status='DEPOSITED'
            )

            # Record transaction
            Transaction.objects.create(
                user=request.user,
                transaction_type='DEPOSIT_NFT',
                related_nft=nft,
                description=f'Deposited NFT {collection.name} #{token_id}'
            )

            messages.success(request, f'NFT {name} deposited successfully!')
            return redirect('nft_list')

        except NFTCollection.DoesNotExist:
            messages.error(request, 'Invalid collection selected!')
        except Exception as e:
            messages.error(request, f'Error depositing NFT: {str(e)}')

    collections = NFTCollection.objects.filter(is_active=True)
    return render(request, 'mosaical_platform/deposit_nft.html', {'collections': collections})

@login_required
def loan_list(request):
    """List user's loans"""
    user_loans = Loan.objects.filter(borrower=request.user)
    return render(request, 'mosaical_platform/loan_list.html', {'user_loans': user_loans})

@login_required
def create_loan(request):
    """Create a new loan against NFT collateral"""
    if request.method == 'POST':
        nft_id = request.POST.get('nft_id')
        loan_amount = Decimal(request.POST.get('loan_amount', '0'))

        try:
            nft = NFTVault.objects.get(id=nft_id, owner=request.user, status='DEPOSITED')

            # Calculate maximum loan amount based on LTV
            max_loan = (nft.estimated_value * nft.collection.max_ltv_ratio) / 100

            if loan_amount > max_loan:
                messages.error(request, f'Loan amount exceeds maximum allowed: {max_loan} vBTC')
                return redirect('create_loan')

            # Create loan
            with transaction.atomic():
                loan = Loan.objects.create(
                    borrower=request.user,
                    nft_collateral=nft,
                    principal_amount=loan_amount,
                    current_debt=loan_amount,
                    ltv_ratio=(loan_amount / nft.estimated_value) * 100,
                    interest_rate=Decimal('5.00')  # Default 5% monthly
                )

                # Update NFT status
                nft.status = 'COLLATERALIZED'
                nft.save()

                # Add vBTC to user balance
                profile = UserProfile.objects.get(user=request.user)
                profile.vbtc_balance += loan_amount
                profile.save()

                # Record transaction
                Transaction.objects.create(
                    user=request.user,
                    transaction_type='LOAN_CREATE',
                    amount=loan_amount,
                    related_nft=nft,
                    related_loan=loan,
                    description=f'Created loan of {loan_amount} vBTC against {nft.collection.name} #{nft.token_id}'
                )

            messages.success(request, f'Loan of {loan_amount} vBTC created successfully!')
            return redirect('loan_list')

        except NFTVault.DoesNotExist:
            messages.error(request, 'Invalid NFT selected!')
        except Exception as e:
            messages.error(request, f'Error creating loan: {str(e)}')

    # Get available NFTs for collateral
    available_nfts = NFTVault.objects.filter(owner=request.user, status='DEPOSITED')
    return render(request, 'mosaical_platform/create_loan.html', {'available_nfts': available_nfts})

@login_required
def hidden_faucet(request):
    """Hidden faucet endpoint - only accessible via secret URL"""
    secret_key = request.GET.get('key')

    # Check if the secret key is valid (you can set this in admin)
    try:
        expected_key = SystemSettings.objects.get(key='FAUCET_SECRET_KEY').value
    except SystemSettings.DoesNotExist:
        expected_key = 'MOSAICAL_DEVPROS_2025'  # Default key

    if secret_key != expected_key:
        raise Http404("Page not found")

    if not request.user.is_authenticated:
        messages.error(request, 'You must be logged in to use the faucet.')
        return redirect('login')

    # Check daily limit
    today = timezone.now().date()
    today_claims = FaucetClaim.objects.filter(
        user=request.user,
        claimed_at__date=today
    ).count()

    if today_claims >= 1:  # Limit 1 claim per day
        messages.error(request, 'You have already claimed from faucet today!')
        return render(request, 'mosaical_platform/faucet.html', {'can_claim': False})

    if request.method == 'POST':
        faucet_amount = Decimal('10.0')  # 10 vBTC per claim

        with transaction.atomic():
            # Add vBTC to user balance
            profile, created = UserProfile.objects.get_or_create(user=request.user)
            profile.vbtc_balance += faucet_amount
            profile.save()

            # Record faucet claim
            FaucetClaim.objects.create(
                user=request.user,
                amount=faucet_amount,
                ip_address=request.META.get('REMOTE_ADDR', '127.0.0.1')
            )

            # Record transaction
            Transaction.objects.create(
                user=request.user,
                transaction_type='FAUCET_CLAIM',
                amount=faucet_amount,
                description=f'Claimed {faucet_amount} vBTC from faucet'
            )

        messages.success(request, f'Successfully claimed {faucet_amount} vBTC!')
        return redirect('dashboard')

    return render(request, 'mosaical_platform/faucet.html', {'can_claim': True})

@login_required
def dpo_marketplace(request):
    """DPO Token Marketplace"""
    available_dpos = DPOToken.objects.filter(is_for_sale=True).exclude(owner=request.user)
    user_dpos = DPOToken.objects.filter(owner=request.user)
    user_nfts = NFTVault.objects.filter(owner=request.user, status='DEPOSITED')

    return render(request, 'mosaical_platform/dpo_marketplace.html', {
        'available_dpos': available_dpos,
        'user_dpos': user_dpos,
        'user_nfts': user_nfts
    })

@login_required
def create_dpo(request):
    """Create a new DPO token"""
    if request.method == 'POST':
        nft_id = request.POST.get('nft_id')
        ownership_percentage = Decimal(request.POST.get('ownership_percentage'))
        price = Decimal(request.POST.get('price'))

        nft = get_object_or_404(NFTVault, id=nft_id, owner=request.user)

        # Check if total DPO ownership doesn't exceed 100%
        existing_dpos = DPOToken.objects.filter(original_nft=nft)
        total_existing = sum(dpo.ownership_percentage for dpo in existing_dpos)

        if total_existing + ownership_percentage > 100:
            messages.error(request, 'Total DPO ownership cannot exceed 100%')
            return redirect('dpo_marketplace')

        # Create DPO token
        DPOToken.objects.create(
            original_nft=nft,
            owner=request.user,
            ownership_percentage=ownership_percentage,
            purchase_price=price,
            current_price=price,
            is_for_sale=True
        )

        # Record transaction
        Transaction.objects.create(
            user=request.user,
            transaction_type='DPO_CREATED',
            amount=Decimal('0'),
            related_nft=nft,
            description=f'Created DPO token: {ownership_percentage}% of {nft.collection.name} #{nft.token_id}'
        )

        messages.success(request, 'DPO token created successfully!')

    return redirect('dpo_marketplace')

@login_required
def buy_dpo(request):
    """Buy a DPO token"""
    if request.method == 'POST':
        dpo_id = request.POST.get('dpo_id')
        dpo = get_object_or_404(DPOToken, id=dpo_id, is_for_sale=True)

        if dpo.owner == request.user:
            messages.error(request, 'You cannot buy your own DPO token')
            return redirect('dpo_marketplace')

        profile = request.user.userprofile
        if profile.vbtc_balance < dpo.current_price:
            messages.error(request, 'Insufficient vBTC balance')
            return redirect('dpo_marketplace')

        with transaction.atomic():
            # Transfer payment
            profile.vbtc_balance -= dpo.current_price
            profile.save()

            seller_profile = dpo.owner.userprofile
            seller_profile.vbtc_balance += dpo.current_price
            seller_profile.save()

            # Transfer ownership
            dpo.owner = request.user
            dpo.purchase_price = dpo.current_price
            dpo.is_for_sale = False
            dpo.save()

            # Record transactions
            Transaction.objects.create(
                user=request.user,
                transaction_type='DPO_PURCHASE',
                amount=dpo.current_price,
                related_nft=dpo.original_nft,
                description=f'Purchased DPO token: {dpo.ownership_percentage}% of {dpo.original_nft.collection.name} #{dpo.original_nft.token_id}'
            )

            Transaction.objects.create(
                user=seller_profile.user,
                transaction_type='DPO_SALE',
                amount=dpo.current_price,
                related_nft=dpo.original_nft,
                description=f'Sold DPO token: {dpo.ownership_percentage}% of {dpo.original_nft.collection.name} #{dpo.original_nft.token_id}'
            )

        messages.success(request, 'DPO token purchased successfully!')

    return redirect('dpo_marketplace')

@login_required
def update_dpo_price(request):
    """Update DPO token price"""
    if request.method == 'POST':
        dpo_id = request.POST.get('dpo_id')
        new_price = Decimal(request.POST.get('new_price'))

        dpo = get_object_or_404(DPOToken, id=dpo_id, owner=request.user)
        dpo.current_price = new_price
        dpo.is_for_sale = True
        dpo.save()

        messages.success(request, 'DPO token price updated successfully!')

    return redirect('dpo_marketplace')

@login_required
def refinance_loan(request):
    """Refinance existing loan with new terms"""
    if request.method == 'POST':
        loan_id = request.POST.get('loan_id')
        new_interest_rate = Decimal(request.POST.get('new_interest_rate', '5.0'))
        additional_amount = Decimal(request.POST.get('additional_amount', '0'))

        try:
            loan = Loan.objects.get(id=loan_id, borrower=request.user, status='ACTIVE')
            nft = loan.nft_collateral

            # Update valuations first
            from .valuation_oracle import ValuationOracle
            nft.estimated_value = ValuationOracle.calculate_dynamic_value(nft)
            nft.save()

            # Calculate new maximum loan amount
            max_total_loan = (nft.estimated_value * nft.collection.max_ltv_ratio) / 100
            new_total_debt = loan.current_debt + additional_amount

            if new_total_debt > max_total_loan:
                messages.error(request, f'Total debt would exceed maximum: {max_total_loan:.6f} vBTC')
                return redirect('loan_list')

            with transaction.atomic():
                # Create new loan record
                new_loan = Loan.objects.create(
                    borrower=request.user,
                    nft_collateral=nft,
                    principal_amount=new_total_debt,
                    current_debt=new_total_debt,
                    ltv_ratio=(new_total_debt / nft.estimated_value) * 100,
                    interest_rate=new_interest_rate
                )

                # Mark old loan as refinanced
                loan.status = 'REPAID'
                loan.save()

                # Add additional funds to user if any
                if additional_amount > 0:
                    profile = UserProfile.objects.get(user=request.user)
                    profile.vbtc_balance += additional_amount
                    profile.save()

                # Record transaction
                Transaction.objects.create(
                    user=request.user,
                    transaction_type='LOAN_CREATE',
                    amount=additional_amount,
                    related_nft=nft,
                    related_loan=new_loan,
                    description=f'Refinanced loan #{loan.id} to #{new_loan.id} with {new_interest_rate}% rate'
                )

            messages.success(request, f'Loan refinanced successfully! New rate: {new_interest_rate}%')

        except Loan.DoesNotExist:
            messages.error(request, 'Invalid loan selected!')
        except Exception as e:
            messages.error(request, f'Error refinancing loan: {str(e)}')

    return redirect('loan_list')

@login_required
def repay_loan(request):
    """Repay loan (partial or full)"""
    if request.method == 'POST':
        loan_id = request.POST.get('loan_id')
        repay_amount = Decimal(request.POST.get('repay_amount', '0'))

        try:
            loan = Loan.objects.get(id=loan_id, borrower=request.user, status='ACTIVE')
            profile = UserProfile.objects.get(user=request.user)

            if repay_amount <= 0 or repay_amount > loan.current_debt:
                messages.error(request, 'Invalid repayment amount!')
                return redirect('loan_list')

            if profile.vbtc_balance < repay_amount:
                messages.error(request, 'Insufficient vBTC balance!')
                return redirect('loan_list')

            with transaction.atomic():
                # Deduct from user balance
                profile.vbtc_balance -= repay_amount
                profile.save()

                # Update loan debt
                loan.current_debt -= repay_amount

                if loan.current_debt <= Decimal('0.00000001'):  # Fully repaid
                    loan.current_debt = Decimal('0')
                    loan.status = 'REPAID'

                    # Release NFT collateral
                    loan.nft_collateral.status = 'DEPOSITED'
                    loan.nft_collateral.save()

                loan.save()

                # Record transaction
                Transaction.objects.create(
                    user=request.user,
                    transaction_type='LOAN_REPAY',
                    amount=repay_amount,
                    related_nft=loan.nft_collateral,
                    related_loan=loan,
                    description=f'Repaid {repay_amount} vBTC for loan #{loan.id}'
                )

            if loan.status == 'REPAID':
                messages.success(request, f'Loan #{loan.id} fully repaid! NFT collateral released.')
            else:
                messages.success(request, f'Partial repayment of {repay_amount} vBTC completed.')

        except Loan.DoesNotExist:
            messages.error(request, 'Invalid loan selected!')
        except Exception as e:
            messages.error(request, f'Error processing repayment: {str(e)}')

    return redirect('loan_list')


@login_required
def get_notifications(request):
    """Get user notifications via AJAX"""
    notifications = NotificationManager.get_user_notifications(request.user)
    return JsonResponse({'notifications': notifications})

@login_required
def mark_notification_read(request):
    """Mark notification as read"""
    if request.method == 'POST':
        notification_id = request.POST.get('notification_id')
        NotificationManager.mark_as_read(request.user, notification_id)
        return JsonResponse({'success': True})
    return JsonResponse({'success': False})

@login_required
def transaction_history(request):
    """Advanced transaction history with filtering"""
    transactions = Transaction.objects.filter(user=request.user).order_by('-created_at')

    # Apply filters
    transaction_type = request.GET.get('type')
    if transaction_type:
        transactions = transactions.filter(transaction_type=transaction_type)

    date_from = request.GET.get('date_from')
    if date_from:
        transactions = transactions.filter(created_at__date__gte=date_from)

    date_to = request.GET.get('date_to')
    if date_to:
        transactions = transactions.filter(created_at__date__lte=date_to)

    # Pagination
    paginator = Paginator(transactions, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'mosaical_platform/transaction_history.html', {
        'transactions': page_obj
    })

@login_required
def export_transactions(request):
    """Export user transactions to CSV"""
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="transactions.csv"'

    writer = csv.writer(response)
    writer.writerow(['Date', 'Type', 'Amount', 'NFT', 'Description'])

    transactions = Transaction.objects.filter(user=request.user).order_by('-created_at')
    for transaction in transactions:
        writer.writerow([
            transaction.created_at.strftime('%Y-%m-%d %H:%M'),
            transaction.get_transaction_type_display(),
            transaction.amount or '',
            f"{transaction.related_nft.collection.name} #{transaction.related_nft.token_id}" if transaction.related_nft else '',
            transaction.description
        ])

    return response

def onboarding(request):
    """User onboarding tutorial"""
    return render(request, 'mosaical_platform/onboarding.html')

def logout_view(request):
    """Custom logout view that accepts both GET and POST"""
    logout(request)
    messages.success(request, 'You have been logged out successfully!')
    return redirect('home')



@login_required
def swap_collateral(request):
    """Swap NFT collateral between different collections"""
    if request.method == 'POST':
        loan_id = request.POST.get('loan_id')
        new_nft_id = request.POST.get('new_nft_id')

        try:
            loan = Loan.objects.get(id=loan_id, borrower=request.user, status='ACTIVE')
            new_nft = NFTVault.objects.get(id=new_nft_id, owner=request.user, status='DEPOSITED')
            old_nft = loan.nft_collateral

            # Update valuations first
            from .valuation_oracle import ValuationOracle
            new_nft.estimated_value = ValuationOracle.calculate_dynamic_value(new_nft)
            new_nft.save()

            # Check if new NFT can support current debt
            max_loan = (new_nft.estimated_value * new_nft.collection.max_ltv_ratio) / 100
            if loan.current_debt > max_loan:
                messages.error(request, f'New NFT cannot support current debt. Max: {max_loan:.6f} vBTC')
                return redirect('loan_list')

            with transaction.atomic():
                # Release old collateral
                old_nft.status = 'DEPOSITED'
                old_nft.save()

                # Set new collateral
                new_nft.status = 'COLLATERALIZED'
                new_nft.save()

                # Update loan
                loan.nft_collateral = new_nft
                loan.ltv_ratio = (loan.current_debt / new_nft.estimated_value) * 100
                loan.save()

                # Record transaction
                Transaction.objects.create(
                    user=request.user,
                    transaction_type='LOAN_CREATE',
                    amount=Decimal('0'),
                    related_nft=new_nft,
                    related_loan=loan,
                    description=f'Swapped collateral from {old_nft.collection.name} #{old_nft.token_id} to {new_nft.collection.name} #{new_nft.token_id}'
                )

            messages.success(request, 'Collateral swapped successfully!')

        except (Loan.DoesNotExist, NFTVault.DoesNotExist):
            messages.error(request, 'Invalid loan or NFT selected!')
        except Exception as e:
            messages.error(request, f'Error swapping collateral: {str(e)}')

    return redirect('loan_list')

def ai_market_intelligence(request):
    """AI Market Intelligence Dashboard"""
    try:
        # Generate market report
        report = MarketIntelligence.generate_market_report(days=30)

        context = {
            'report': report,
            'user_portfolio': None
        }

        # Add user portfolio analysis if authenticated
        if request.user.is_authenticated:
            context['user_portfolio'] = MarketIntelligence.get_user_portfolio_analysis(request.user)

        return render(request, 'mosaical_platform/ai_market_intelligence.html', context)

    except Exception as e:
        messages.error(request, f'Error generating market intelligence: {e}')
        return redirect('dashboard')

@csrf_exempt
def test_ai_prediction(request, nft_id):
    """Test AI prediction for specific NFT"""
    if request.method == 'POST':
        try:
            nft = get_object_or_404(NFTVault, id=nft_id)

            # Get AI prediction
            predicted_price = nft_predictor.predict_price(nft)
            current_price = nft.estimated_value
            change_percent = float((predicted_price - current_price) / current_price * 100)

            # Get confidence interval
            confidence_interval = nft_predictor.calculate_confidence_interval(nft, predicted_price)

            # Detect anomalies
            anomalies = nft_predictor.detect_anomalies(nft, predicted_price)

            # Get model status
            model_status = {
                'is_trained': nft_predictor.is_trained,
                'random_forest': True,
                'gradient_boosting': True,
                'xgboost': nft_predictor.xgboost is not None
            }

            return JsonResponse({
                'success': True,
                'nft_info': {
                    'id': nft.id,
                    'name': f"{nft.collection.name} #{nft.token_id}",
                    'collection': nft.collection.name
                },
                'current_price': float(current_price),
                'predicted_price': float(predicted_price),
                'change_percent': round(change_percent, 2),
                'confidence_interval': {
                    'lower': float(confidence_interval['lower_bound']),
                    'upper': float(confidence_interval['upper_bound']),
                    'level': confidence_interval['confidence_level']
                },
                'anomalies': anomalies,
                'model_status': model_status,
                'prediction_method': 'ensemble' if nft_predictor.is_trained else 'heuristic'
            })

        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': str(e)
            })

    return JsonResponse({'success': False, 'error': 'POST method required'})

def ai_training_status(request):
    """Get AI model training status"""
    status = {
        'is_trained': nft_predictor.is_trained,
        'models': {
            'random_forest': hasattr(nft_predictor, 'random_forest'),
            'gradient_boosting': hasattr(nft_predictor, 'gradient_boosting'),
            'xgboost': nft_predictor.xgboost is not None
        },
        'ensemble_weights': nft_predictor.ensemble_weights
    }

    return JsonResponse(status)

@login_required
def train_models(request):
    """Train AI models using existing data"""
    if request.method == 'POST':
        try:
            # Train the models
            result = nft_predictor.train_models()
            messages.success(request, f'Models trained successfully! Features: {result["feature_count"]}, Samples: {result["sample_count"]}')
        except Exception as e:
            messages.error(request, f'Error training models: {str(e)}')

    return redirect('ai_market_intelligence')